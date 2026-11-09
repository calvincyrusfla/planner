(function () {
  'use strict';

  var STORAGE_KEY = 'study:geology-ch5';
  var TABS = ['notes', 'flashcards', 'quiz'];
  var DEFAULT_TAB = 'notes';
  var MARK_GOT = 'got';
  var MARK_LEARNING = 'learning';

  function byId(id) {
    return document.getElementById(id);
  }

  function isPlainObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function hasModifier(event) {
    return event.altKey || event.ctrlKey || event.metaKey || event.shiftKey;
  }

  function loadProgress() {
    var progress = null;
    try {
      progress = JSON.parse(window.localStorage.getItem(STORAGE_KEY));
    } catch (err) {
      progress = null;
    }
    if (!isPlainObject(progress)) {
      progress = {};
    }
    if (!isPlainObject(progress.flashcards)) {
      progress.flashcards = {};
    }
    return progress;
  }

  function saveProgress(progress) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(progress));
    } catch (err) {
      // Storage is blocked or full: progress lasts for this visit only.
    }
  }

  function shuffled(items) {
    var result = items.slice();
    for (var i = result.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var swap = result[i];
      result[i] = result[j];
      result[j] = swap;
    }
    return result;
  }

  function initHeader(data) {
    if (data.title) {
      document.title = data.title;
      byId('guide-title').textContent = data.title;
    }
    if (data.source) {
      var source = byId('guide-source');
      source.textContent = 'Source: ' + data.source;
      source.hidden = false;
    }
  }

  function initTabs(progress) {
    var current = TABS.indexOf(progress.tab) !== -1 ? progress.tab : DEFAULT_TAB;

    function activate(name, moveFocus) {
      current = name;
      TABS.forEach(function (tabName) {
        var selected = tabName === name;
        var tab = byId('tab-' + tabName);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        byId('panel-' + tabName).hidden = !selected;
      });
      if (moveFocus) {
        byId('tab-' + name).focus();
      }
      if (progress.tab !== name) {
        progress.tab = name;
        saveProgress(progress);
      }
    }

    TABS.forEach(function (tabName) {
      byId('tab-' + tabName).addEventListener('click', function () {
        activate(tabName, false);
      });
    });

    byId('tablist').addEventListener('keydown', function (event) {
      if (hasModifier(event)) {
        return;
      }
      var index = TABS.indexOf(current);
      var next = -1;
      if (event.key === 'ArrowRight') {
        next = (index + 1) % TABS.length;
      } else if (event.key === 'ArrowLeft') {
        next = (index - 1 + TABS.length) % TABS.length;
      } else if (event.key === 'Home') {
        next = 0;
      } else if (event.key === 'End') {
        next = TABS.length - 1;
      }
      if (next !== -1) {
        event.preventDefault();
        activate(TABS[next], true);
      }
    });

    activate(current, false);
  }

  function initFlashcards(concepts, progress) {
    var marks = progress.flashcards;
    var panel = byId('panel-flashcards');
    var els = {
      position: byId('fc-position'),
      tally: byId('fc-tally'),
      onlyLearning: byId('fc-only-learning'),
      empty: byId('fc-empty'),
      deck: byId('fc-deck'),
      card: byId('fc-card'),
      side: byId('fc-side'),
      text: byId('fc-text'),
      got: byId('fc-got'),
      learning: byId('fc-learning'),
      prev: byId('fc-prev'),
      next: byId('fc-next'),
      shuffle: byId('fc-shuffle')
    };
    var order = concepts.slice();
    var onlyLearning = false;
    var deck = [];
    var pos = 0;
    var flipped = false;

    function currentConcept() {
      return deck[pos] || null;
    }

    function rebuildDeck() {
      deck = onlyLearning
        ? order.filter(function (concept) { return marks[concept.id] === MARK_LEARNING; })
        : order.slice();
      if (pos >= deck.length) {
        pos = Math.max(deck.length - 1, 0);
      }
    }

    function countMarks(value) {
      return concepts.filter(function (concept) { return marks[concept.id] === value; }).length;
    }

    function renderMarkButton(button, active) {
      button.setAttribute('aria-pressed', String(active));
      button.classList.toggle('is-active', active);
    }

    function render() {
      var concept = currentConcept();
      els.tally.textContent = 'Got it: ' + countMarks(MARK_GOT) + ' \u00b7 Still learning: ' + countMarks(MARK_LEARNING);
      els.deck.hidden = !concept;
      els.empty.hidden = !!concept;
      if (!concept) {
        els.position.textContent = 'No cards';
        els.empty.textContent = onlyLearning
          ? 'No cards are marked \u201cStill learning\u201d.'
          : 'There are no flashcards in this guide.';
        return;
      }
      els.position.textContent = 'Card ' + (pos + 1) + ' of ' + deck.length;
      els.side.textContent = flipped ? 'Definition' : 'Term';
      els.text.textContent = flipped ? concept.definition : concept.term;
      els.card.classList.toggle('is-flipped', flipped);
      renderMarkButton(els.got, marks[concept.id] === MARK_GOT);
      renderMarkButton(els.learning, marks[concept.id] === MARK_LEARNING);
    }

    function flip() {
      if (currentConcept()) {
        flipped = !flipped;
        render();
      }
    }

    function step(delta) {
      if (deck.length) {
        pos = (pos + delta + deck.length) % deck.length;
        flipped = false;
        render();
      }
    }

    function shuffleDeck() {
      order = shuffled(order);
      pos = 0;
      flipped = false;
      rebuildDeck();
      render();
    }

    // Pressing the active marker again clears it.
    function setMark(value) {
      var concept = currentConcept();
      if (!concept) {
        return;
      }
      if (marks[concept.id] === value) {
        delete marks[concept.id];
      } else {
        marks[concept.id] = value;
      }
      saveProgress(progress);
      if (onlyLearning && marks[concept.id] !== MARK_LEARNING) {
        flipped = false;
        rebuildDeck();
        if (!deck.length) {
          els.onlyLearning.focus();
        }
      }
      render();
    }

    function setOnlyLearning(value) {
      var concept = currentConcept();
      onlyLearning = value;
      rebuildDeck();
      var index = concept ? deck.indexOf(concept) : -1;
      if (index !== -1) {
        pos = index;
      } else {
        pos = 0;
        flipped = false;
      }
      render();
    }

    function onKeydown(event) {
      if (panel.hidden || hasModifier(event)) {
        return;
      }
      var target = event.target;
      if (event.key === ' ') {
        // Focused controls keep their own Space behaviour; the card button flips through its click.
        if (event.repeat || target.closest('a, button, input, select, textarea, summary, [contenteditable]')) {
          return;
        }
        event.preventDefault();
        flip();
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        if (target.closest('select, textarea, [contenteditable], [role="tab"], input:not([type="checkbox"])')) {
          return;
        }
        event.preventDefault();
        step(event.key === 'ArrowLeft' ? -1 : 1);
      }
    }

    els.card.addEventListener('click', flip);
    els.prev.addEventListener('click', function () { step(-1); });
    els.next.addEventListener('click', function () { step(1); });
    els.shuffle.addEventListener('click', shuffleDeck);
    els.got.addEventListener('click', function () { setMark(MARK_GOT); });
    els.learning.addEventListener('click', function () { setMark(MARK_LEARNING); });
    els.onlyLearning.addEventListener('change', function () { setOnlyLearning(els.onlyLearning.checked); });
    document.addEventListener('keydown', onKeydown);

    els.onlyLearning.checked = false;
    rebuildDeck();
    render();
  }

  function makeEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function isValidQuestion(question) {
    return isPlainObject(question) &&
      Array.isArray(question.choices) &&
      question.answer >= 0 &&
      question.answer < question.choices.length &&
      Math.floor(question.answer) === question.answer;
  }

  function jumpToConcept(id) {
    byId('tab-notes').click();
    // A card hidden by the search box could not take focus or scroll into view.
    var search = byId('notes-search');
    if (search) {
      search.value = '';
      filterNotes('');
    }
    var card = byId('concept-' + id);
    if (!card) {
      return;
    }
    Array.prototype.forEach.call(document.querySelectorAll('.note-card.is-target'), function (node) {
      node.classList.remove('is-target');
    });
    card.classList.add('is-target');
    card.focus();
    card.scrollIntoView({ block: 'start' });
  }

  function initNotes(concepts) {
    var root = byId('notes-root');
    root.textContent = '';
    if (!concepts.length) {
      root.appendChild(makeEl('p', 'placeholder', 'There are no notes in this guide.'));
      return;
    }
    concepts.forEach(function (concept) {
      var card = makeEl('article', 'note-card');
      card.id = 'concept-' + concept.id;
      card.tabIndex = -1;
      card.appendChild(makeEl('h2', 'note-term', concept.term));
      card.appendChild(makeEl('p', 'note-definition', concept.definition));
      if (Array.isArray(concept.details) && concept.details.length) {
        var list = makeEl('ul', 'note-details');
        concept.details.forEach(function (detail) {
          list.appendChild(makeEl('li', '', detail));
        });
        card.appendChild(list);
      }
      root.appendChild(card);
    });
  }

  function filterNotes(rawQuery) {
    var query = rawQuery.trim().toLowerCase();
    var cards = document.querySelectorAll('.note-card');
    var shown = 0;
    Array.prototype.forEach.call(cards, function (card) {
      var match = !query || ['.note-term', '.note-definition'].some(function (selector) {
        return card.querySelector(selector).textContent.toLowerCase().indexOf(query) !== -1;
      });
      card.hidden = !match;
      if (match) {
        shown += 1;
      }
    });
    var status = byId('notes-search-status');
    if (!query) {
      status.textContent = '';
    } else if (shown) {
      status.textContent = 'Showing ' + shown + ' of ' + cards.length + ' concepts';
    } else {
      status.textContent = 'No concepts match your search.';
    }
  }

  function initNoteSearch() {
    var root = byId('notes-root');
    if (!root.querySelector('.note-card')) {
      return;
    }
    var block = makeEl('div', 'notes-search');
    var label = makeEl('label', 'notes-search-label', 'Search by term or definition');
    label.htmlFor = 'notes-search';
    var input = document.createElement('input');
    input.type = 'search';
    input.id = 'notes-search';
    input.className = 'notes-search-input';
    input.autocomplete = 'off';
    input.addEventListener('input', function () {
      filterNotes(input.value);
    });
    var status = makeEl('p', 'notes-search-status');
    status.id = 'notes-search-status';
    status.setAttribute('role', 'status');
    block.appendChild(label);
    block.appendChild(input);
    block.appendChild(status);
    root.insertBefore(block, root.firstChild);
  }

  // Stays out of the screen layout; the print stylesheet reveals it.
  function buildAnswerKey(questions) {
    if (!questions.length) {
      return;
    }
    var section = makeEl('section', 'answer-key');
    section.appendChild(makeEl('h2', '', 'Answer key'));
    var list = makeEl('ol', 'answer-key-list');
    questions.forEach(function (question) {
      var item = makeEl('li');
      if (typeof question.n === 'number') {
        item.value = question.n;
      }
      item.appendChild(makeEl('p', 'answer-key-prompt', question.prompt));
      item.appendChild(makeEl('p', 'answer-key-answer', 'Answer: ' + question.choices[question.answer]));
      if (question.explanation) {
        item.appendChild(makeEl('p', '', question.explanation));
      }
      if (question.verify) {
        item.appendChild(makeEl('p', 'quiz-verify', 'Check this answer against your graded quiz'));
      }
      list.appendChild(item);
    });
    section.appendChild(list);
    byId('app').appendChild(section);
  }

  function initQuiz(questions, concepts, progress) {
    var panel = byId('panel-quiz');
    panel.textContent = '';
    if (!questions.length) {
      panel.appendChild(makeEl('p', 'placeholder', 'There are no quiz questions in this guide.'));
      return;
    }

    var termsById = Object.create(null);
    concepts.forEach(function (concept) {
      termsById[concept.id] = concept.term;
    });

    var shuffleQuestions = false;
    var shuffleChoices = false;
    var roundSource = questions;
    var roundIsFull = true;
    var queue = [];
    var pos = 0;
    var score = 0;
    var missed = [];
    var answered = false;
    var finished = false;

    var status = makeEl('p', 'quiz-status');
    var stage = makeEl('div', 'quiz-stage');

    function addOption(text, onChange) {
      var label = makeEl('label', 'quiz-option');
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.addEventListener('change', function () {
        onChange(input.checked);
        // Once a round is under way the new setting waits for the next one.
        if (!finished && pos === 0 && !answered) {
          startRound(roundSource, roundIsFull, false);
        }
      });
      label.appendChild(input);
      label.appendChild(makeEl('span', '', text));
      return label;
    }

    function updateStatus() {
      status.hidden = false;
      status.textContent = 'Question ' + (pos + 1) + ' of ' + queue.length + ' \u00b7 Score: ' + score;
    }

    function startRound(source, isFull, moveFocus) {
      roundSource = source;
      roundIsFull = isFull;
      queue = (shuffleQuestions ? shuffled(source) : source.slice()).map(function (question) {
        var choices = question.choices.map(function (text, index) {
          return { text: text, correct: index === question.answer };
        });
        return { question: question, choices: shuffleChoices && question.type !== 'tf' ? shuffled(choices) : choices };
      });
      pos = 0;
      score = 0;
      missed = [];
      finished = false;
      renderQuestion(moveFocus);
    }

    function makeConceptLinks(question) {
      var ids = (question.concepts || []).filter(function (id) { return id in termsById; });
      if (!ids.length) {
        return null;
      }
      var block = makeEl('div', 'quiz-concepts');
      block.appendChild(makeEl('p', 'quiz-concepts-label', 'Related concepts:'));
      var links = makeEl('div', 'quiz-links');
      ids.forEach(function (id) {
        var link = makeEl('a', 'quiz-link', termsById[id]);
        link.href = '#concept-' + encodeURIComponent(id);
        link.addEventListener('click', function (event) {
          event.preventDefault();
          jumpToConcept(id);
        });
        links.appendChild(link);
      });
      block.appendChild(links);
      return block;
    }

    function makeFeedback(question, isCorrect) {
      var feedback = makeEl('div', 'quiz-feedback ' + (isCorrect ? 'is-right' : 'is-wrong'));
      feedback.tabIndex = -1;
      feedback.appendChild(makeEl('p', 'quiz-result', isCorrect ? 'Correct' : 'Incorrect'));
      if (question.explanation) {
        feedback.appendChild(makeEl('p', 'quiz-explanation', question.explanation));
      }
      if (question.verify) {
        feedback.appendChild(makeEl('p', 'quiz-verify', 'Check this answer against your graded quiz'));
      }
      var links = makeConceptLinks(question);
      if (links) {
        feedback.appendChild(links);
      }
      var actions = makeEl('div', 'quiz-actions');
      var next = makeEl('button', 'quiz-btn quiz-btn-primary', pos + 1 < queue.length ? 'Next question' : 'See results');
      next.type = 'button';
      next.addEventListener('click', function () {
        pos += 1;
        if (pos < queue.length) {
          renderQuestion(true);
        } else {
          renderEnd();
        }
      });
      actions.appendChild(next);
      feedback.appendChild(actions);
      return feedback;
    }

    function renderQuestion(moveFocus) {
      var item = queue[pos];
      answered = false;
      stage.textContent = '';
      updateStatus();

      var form = makeEl('form', 'quiz-form');
      var fieldset = makeEl('fieldset', 'quiz-fieldset');
      fieldset.tabIndex = -1;
      fieldset.appendChild(makeEl('legend', 'quiz-prompt', item.question.prompt));
      var rows = item.choices.map(function (choice, index) {
        var label = makeEl('label', 'quiz-choice');
        var input = document.createElement('input');
        input.type = 'radio';
        input.name = 'quiz-choice';
        input.value = String(index);
        label.appendChild(input);
        label.appendChild(makeEl('span', 'quiz-choice-text', choice.text));
        fieldset.appendChild(label);
        return { label: label, input: input, choice: choice };
      });
      form.appendChild(fieldset);

      var message = makeEl('p', 'quiz-message');
      message.setAttribute('role', 'alert');
      form.appendChild(message);

      var actions = makeEl('div', 'quiz-actions');
      var submit = makeEl('button', 'quiz-btn quiz-btn-primary', 'Submit');
      submit.type = 'submit';
      actions.appendChild(submit);
      form.appendChild(actions);

      form.addEventListener('submit', function (event) {
        event.preventDefault();
        if (answered) {
          return;
        }
        var checked = form.querySelector('input:checked');
        if (!checked) {
          message.textContent = 'Choose an answer first.';
          return;
        }
        answered = true;
        var picked = item.choices[Number(checked.value)];
        rows.forEach(function (row) {
          row.input.disabled = true;
          if (row.choice.correct) {
            row.label.classList.add('is-correct');
            row.label.appendChild(makeEl('span', 'quiz-tag', 'Correct answer'));
          } else if (row.choice === picked) {
            row.label.classList.add('is-wrong');
            row.label.appendChild(makeEl('span', 'quiz-tag', 'Your answer'));
          }
        });
        if (picked.correct) {
          score += 1;
        } else {
          missed.push(item.question);
        }
        message.textContent = '';
        form.removeChild(actions);
        updateStatus();
        var feedback = makeFeedback(item.question, picked.correct);
        stage.appendChild(feedback);
        feedback.focus();
      });

      stage.appendChild(form);
      if (moveFocus) {
        fieldset.focus();
      }
    }

    function readBest() {
      var best = progress.quizBest;
      return isPlainObject(best) && best.total > 0 && best.score >= 0 ? best : null;
    }

    // Retry rounds cover only missed questions, so they never count as a best score.
    function recordBest() {
      var best = readBest();
      if (roundIsFull && (!best || score / queue.length > best.score / best.total)) {
        progress.quizBest = { score: score, total: queue.length };
        saveProgress(progress);
      }
    }

    function renderEnd() {
      finished = true;
      recordBest();
      status.hidden = true;
      stage.textContent = '';

      var end = makeEl('div', 'quiz-end');
      var heading = makeEl('h2', 'quiz-end-title', 'Quiz complete');
      heading.tabIndex = -1;
      end.appendChild(heading);
      end.appendChild(makeEl('p', 'quiz-score', 'Score: ' + score + ' out of ' + queue.length));
      end.appendChild(makeEl('p', 'quiz-percent', Math.round(score / queue.length * 100) + '%'));
      var best = readBest();
      if (best) {
        end.appendChild(makeEl('p', 'quiz-best', 'Best full quiz: ' + best.score + ' out of ' + best.total + ' (' + Math.round(best.score / best.total * 100) + '%)'));
      }

      if (missed.length) {
        end.appendChild(makeEl('h3', 'quiz-missed-title', 'Missed questions'));
        var list = makeEl('ul', 'quiz-missed');
        missed.forEach(function (question) {
          var row = makeEl('li');
          row.appendChild(makeEl('p', 'quiz-missed-prompt', question.prompt));
          row.appendChild(makeEl('p', 'quiz-missed-answer', 'Correct answer: ' + question.choices[question.answer]));
          list.appendChild(row);
        });
        end.appendChild(list);
      } else {
        end.appendChild(makeEl('p', '', 'You got every question right.'));
      }

      var actions = makeEl('div', 'quiz-actions');
      if (missed.length) {
        var retry = makeEl('button', 'quiz-btn quiz-btn-primary', 'Retry missed only');
        retry.type = 'button';
        retry.addEventListener('click', function () {
          startRound(missed.slice(), false, true);
        });
        actions.appendChild(retry);
      }
      var restart = makeEl('button', 'quiz-btn', 'Restart');
      restart.type = 'button';
      restart.addEventListener('click', function () {
        startRound(questions, true, true);
      });
      actions.appendChild(restart);
      end.appendChild(actions);

      stage.appendChild(end);
      heading.focus();
    }

    var options = makeEl('div', 'quiz-options');
    options.appendChild(addOption('Shuffle questions', function (value) { shuffleQuestions = value; }));
    options.appendChild(addOption('Shuffle choices', function (value) { shuffleChoices = value; }));
    options.appendChild(makeEl('p', 'quiz-options-note', 'Changes made during a quiz apply when you next restart.'));
    panel.appendChild(options);
    panel.appendChild(status);
    panel.appendChild(stage);

    startRound(questions, true, false);
  }

  function init() {
    var data = window.STUDY_DATA;
    if (!data || !Array.isArray(data.concepts)) {
      byId('load-error').hidden = false;
      return;
    }
    var progress = loadProgress();
    initHeader(data);
    initTabs(progress);
    initFlashcards(data.concepts, progress);
    var questions = (Array.isArray(data.questions) ? data.questions : []).filter(isValidQuestion);
    initNotes(data.concepts);
    initNoteSearch();
    initQuiz(questions, data.concepts, progress);
    buildAnswerKey(questions);
    byId('app').hidden = false;
  }

  init();
}());

if ('serviceWorker' in navigator && location.protocol.indexOf('http') === 0) navigator.serviceWorker.register('sw.js').catch(function(){});
