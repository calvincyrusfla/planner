#!/usr/bin/env bash
# setup_foreman.sh - get the Gemma Shop Foreman (Ollama) ready on macOS/Linux.
#
#   ./foundry/setup_foreman.sh
#
# Steps (safe to run again, no admin rights needed):
#   1. check that `ollama` is on PATH (else print the download link)
#   2. report GPU / VRAM (nvidia-smi, or system_profiler on macOS)
#   3. pick the model: $FOREMAN_MODEL, default gemma4:e4b (suggest gemma4:e2b below 8 GB VRAM)
#   4. start `ollama serve` in the background if the API does not answer
#   5. `ollama pull` the model unless it is already installed
#   6. run `python -m foundry.foreman_probe` with the repo's python
#   7. run `python -m foundry.gemma_doctor` (GPU % and tokens/s, standard library only).
#      A slow or CPU-only Gemma is reported, not treated as a failed setup.
#   8. report whether the GitHub CLI (`gh`) is installed and logged in, for automatic
#      pull requests. Read-only (`gh auth status`); never fails the setup.
#
# Environment:
#   FOREMAN_MODEL        model tag (default gemma4:e4b)
#   FOREMAN_OLLAMA_URL   Ollama server root (default http://localhost:11434)
#   FOREMAN_DRY_RUN=1    print the planned commands, never run ollama or the probe
#   FOREMAN_WAIT_S       seconds to wait for `ollama serve` to answer (default 30)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODEL="${FOREMAN_MODEL:-gemma4:e4b}"
OLLAMA_URL="${FOREMAN_OLLAMA_URL:-http://localhost:11434}"
OLLAMA_URL="${OLLAMA_URL%/}"
OLLAMA_URL="${OLLAMA_URL%/v1}"
DRY_RUN="${FOREMAN_DRY_RUN:-0}"
WAIT_S="${FOREMAN_WAIT_S:-30}"
LOG_FILE="${TMPDIR:-/tmp}/ollama-serve.log"

say() { printf '[foreman] %s\n' "$*"; }

# run CMD...: execute, or only print it in dry-run mode.
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

# api_up: true when the Ollama API answers (read-only HTTP GET, never runs ollama).
api_up() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 2 --noproxy '*' "$OLLAMA_URL/api/version" >/dev/null 2>&1
  else
    "$PYTHON" -c 'import sys,urllib.request as u; u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=2)' \
      "$OLLAMA_URL/api/version" >/dev/null 2>&1
  fi
}

# --- python ---------------------------------------------------------------
PYTHON=""
# First of .venv / python3 / python that is Python 3.11 or newer.
for candidate in "$REPO_ROOT/.venv/bin/python" python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON="$candidate"; break
  fi
done

# --- 1. ollama on PATH ------------------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
  say "Ollama is not installed (no 'ollama' on PATH)."
  say "Download it from https://ollama.com/download, install it, then run this script again."
  exit 1
fi
say "ollama found: $(command -v ollama)"

# --- 2. GPU / VRAM ----------------------------------------------------------
VRAM_MB=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_INFO="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null)"
  if [ -n "$GPU_INFO" ]; then
    while IFS=, read -r name mem; do
      mem="$(printf '%s' "$mem" | tr -dc '0-9')"
      say "GPU: $(printf '%s' "$name" | sed 's/^ *//;s/ *$//') (${mem:-?} MB VRAM)"
      if [ -n "$mem" ] && { [ -z "$VRAM_MB" ] || [ "$mem" -gt "$VRAM_MB" ]; }; then VRAM_MB="$mem"; fi
    done <<EOF
$GPU_INFO
EOF
  fi
elif [ "$(uname -s 2>/dev/null)" = "Darwin" ] && command -v system_profiler >/dev/null 2>&1; then
  system_profiler SPDisplaysDataType 2>/dev/null | grep -E 'Chipset Model|VRAM|Total Number of Cores' | sed 's/^ */[foreman] GPU: /'
  if [ "$(uname -m 2>/dev/null)" = "arm64" ]; then
    MEM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    VRAM_MB=$(( MEM_BYTES / 1048576 ))
    say "Apple Silicon: ${VRAM_MB} MB unified memory shared with the GPU"
  fi
fi
if [ -z "$VRAM_MB" ]; then
  say "No GPU VRAM reading available; Ollama will fall back to CPU if needed."
fi

# --- 3. model -----------------------------------------------------------------
say "Model: $MODEL (set FOREMAN_MODEL to change it)"
if [ -n "$VRAM_MB" ] && [ "$VRAM_MB" -lt 8192 ] && [ "$MODEL" = "gemma4:e4b" ]; then
  say "Only ${VRAM_MB} MB VRAM (< 8 GB): consider FOREMAN_MODEL=gemma4:e2b for a smaller, faster Foreman."
fi

# --- 4. ollama serve --------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  if api_up; then
    say "Ollama API already answering at $OLLAMA_URL"
  else
    say "Ollama API not answering at $OLLAMA_URL; would start it:"
    printf '[dry-run] %s\n' "nohup ollama serve >$LOG_FILE 2>&1 &"
  fi
elif api_up; then
  say "Ollama API already answering at $OLLAMA_URL"
else
  say "Starting 'ollama serve' in the background (log: $LOG_FILE)"
  nohup ollama serve >"$LOG_FILE" 2>&1 &
  waited=0
  until api_up; do
    if [ "$waited" -ge "$WAIT_S" ]; then
      say "Ollama did not answer at $OLLAMA_URL after ${WAIT_S}s. See $LOG_FILE."
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  say "Ollama API is up at $OLLAMA_URL"
fi

# --- 5. ollama pull -----------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  run ollama pull "$MODEL"
elif ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qxF -e "$MODEL" -e "$MODEL:latest"; then
  say "Model $MODEL already installed; skipping pull."
else
  say "Pulling $MODEL (first time can take a while)..."
  if ! ollama pull "$MODEL"; then
    say "'ollama pull $MODEL' failed. Check the tag and your connection, then run again."
    exit 1
  fi
fi

# gemma_doctor: speed check (GPU offload, tokens/s). Never changes the exit status.
gemma_doctor() {
  say "Speed check: is Gemma on the GPU, and how fast? (python -m foundry.gemma_doctor)"
  run "$PYTHON" -m foundry.gemma_doctor --base-url "$OLLAMA_URL" --model "$MODEL"
  doc=$?
  case "$doc" in
    0) ;;
    1) say "Gemma works, but slowly or on the CPU. Setup still succeeded: Foundry falls back to its"
       say "built-in sizing when Gemma is too slow. To speed it up see \"Make Gemma fast\" in foundry/README.md." ;;
    2) say "The speed check could not reach Gemma or find the model. Run it again later:"
       say "  $PYTHON -m foundry.gemma_doctor --model $MODEL" ;;
    *) say "The speed check stopped with exit code $doc. Run it again later: $PYTHON -m foundry.gemma_doctor" ;;
  esac
  return 0
}

# gh_check: is the GitHub CLI installed and logged in (automatic pull requests)?
# Read-only (`gh auth status`, also in dry-run). Never changes the exit status.
gh_check() {
  say "GitHub check: automatic pull requests need the GitHub CLI (gh), logged in."
  if ! command -v gh >/dev/null 2>&1; then
    say "gh is not installed. Foundry still writes patches, but cannot open pull requests."
    say "Install it from https://cli.github.com/ then run: gh auth login"
    say "(Or turn automatic pull requests off: FOUNDRY_AUTO_PR=0.)"
    return 0
  fi
  say "gh found: $(command -v gh)"
  if gh auth status >/dev/null 2>&1; then
    say "gh is logged in: Foundry opens a pull request for every approved task."
  else
    say "gh is not logged in. Run: gh auth login   (then Foundry can open pull requests)"
  fi
  return 0
}

# --- 6. probe -----------------------------------------------------------------
if [ -z "$PYTHON" ]; then
  say "Python 3.11 or newer not found; install it, then run: python3 -m foundry.foreman_probe"
  exit 1
fi
cd "$REPO_ROOT" || exit 1
run "$PYTHON" -m foundry.foreman_probe --base-url "$OLLAMA_URL/v1" --model "$MODEL"
status=$?
if [ "$status" -eq 0 ]; then
  [ "$DRY_RUN" = "1" ] || say "Shop Foreman ready."
  # --- 7. speed check -------------------------------------------------------
  gemma_doctor
  # --- 8. GitHub CLI --------------------------------------------------------
  gh_check
  if [ "$PYTHON" = "$REPO_ROOT/.venv/bin/python" ]; then
    say "Next (from $REPO_ROOT): $PYTHON -m foundry.main --real --repo <path> --title \"...\""
  else
    # The probe ran on a system python; the orchestrator needs pydantic, so set up the venv first.
    say "No .venv yet - the probe ran with $PYTHON. Create the venv first (see \"Install\" in foundry/README.md):"
    say "  python3 -m venv .venv && .venv/bin/python -m pip install -r foundry/requirements.txt"
    say "Then (from $REPO_ROOT): .venv/bin/python -m foundry.main --real --repo <path> --title \"...\""
  fi
fi
exit "$status"
