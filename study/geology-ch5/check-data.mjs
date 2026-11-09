// Validates a study guide data file against the data contract.
// Usage: node study/geology-ch5/check-data.mjs [path/to/data.js]
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const EXPECTED_QUESTIONS = 25;

function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function loadData(file) {
  const source = readFileSync(file, 'utf8');
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: file, timeout: 5000 });
  return sandbox.window.STUDY_DATA;
}

function validateConcepts(concepts, problems) {
  const ids = new Set();
  concepts.forEach((concept, index) => {
    const id = isObject(concept) ? concept.id : undefined;
    if (typeof id !== 'string' || id === '') {
      problems.push(`concept ${index + 1}: id must be a non-empty string`);
    } else if (ids.has(id)) {
      problems.push(`concept ${index + 1}: duplicate id "${id}"`);
    } else {
      ids.add(id);
    }
  });
  return ids;
}

function validateQuestion(question, index, conceptIds, problems) {
  const label = `question ${index + 1}`;
  if (!isObject(question)) {
    problems.push(`${label}: must be an object`);
    return;
  }
  if (question.n !== index + 1) {
    problems.push(`${label}: n must be ${index + 1}, found ${JSON.stringify(question.n)}`);
  }
  if (question.type !== 'mc' && question.type !== 'tf') {
    problems.push(`${label}: type must be "mc" or "tf", found ${JSON.stringify(question.type)}`);
  }

  if (typeof question.prompt !== 'string' || question.prompt.trim() === '') {
    problems.push(`${label}: prompt must not be empty`);
  }

  const { choices, answer } = question;
  if (!Array.isArray(choices) || choices.length === 0) {
    problems.push(`${label}: choices must be a non-empty array`);
  } else {
    if (question.type === 'tf'
      && (choices.length !== 2 || choices[0] !== 'True' || choices[1] !== 'False')) {
      problems.push(`${label}: tf choices must be exactly ["True", "False"]`);
    }
    if (!Number.isInteger(answer) || answer < 0 || answer >= choices.length) {
      problems.push(`${label}: answer must be an integer index within choices, found ${JSON.stringify(answer)}`);
    }
  }

  if (typeof question.explanation !== 'string' || question.explanation.trim() === '') {
    problems.push(`${label}: explanation must not be empty`);
  }

  if (!Array.isArray(question.concepts)) {
    problems.push(`${label}: concepts must be an array of concept ids`);
  } else {
    for (const id of question.concepts) {
      if (!conceptIds.has(id)) {
        problems.push(`${label}: unknown concept id ${JSON.stringify(id)}`);
      }
    }
  }
}

function validate(data) {
  if (!isObject(data)) return ['window.STUDY_DATA is not set to an object'];

  const problems = [];
  const { concepts, questions } = data;
  let conceptIds = new Set();
  if (Array.isArray(concepts)) {
    conceptIds = validateConcepts(concepts, problems);
  } else {
    problems.push('concepts must be an array');
  }

  if (Array.isArray(questions)) {
    if (questions.length !== EXPECTED_QUESTIONS) {
      problems.push(`expected ${EXPECTED_QUESTIONS} questions, found ${questions.length}`);
    }
    questions.forEach((question, index) => {
      validateQuestion(question, index, conceptIds, problems);
    });
  } else {
    problems.push('questions must be an array');
  }
  return problems;
}

function main() {
  const here = dirname(fileURLToPath(import.meta.url));
  const file = process.argv[2] ? resolve(process.argv[2]) : join(here, 'data.js');

  let data;
  let problems;
  try {
    data = loadData(file);
    problems = validate(data);
  } catch (err) {
    problems = [`could not load ${file}: ${err && err.message ? err.message : err}`];
  }

  if (problems.length > 0) {
    for (const problem of problems) console.error(problem);
    process.exitCode = 1;
    return;
  }
  console.log(`OK: ${data.questions.length} questions, ${data.concepts.length} concepts`);
}

main();
