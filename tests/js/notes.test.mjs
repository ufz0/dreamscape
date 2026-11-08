// The state rule for the "no final answer" note, as a plain node test.
// Run: node --test tests/js/notes.test.mjs
import { test } from "node:test";
import assert from "node:assert";

import { canShowNote, noteText } from "../../static/js/notes.js";

const NOTE = "The model produced reasoning but no final answer.";
const CUT_OFF = "The response was cut off by the token limit during reasoning.";

test("the final step with thinking and no answer shows the note", () => {
  const turn = { reasoning: "Thought about it.", content: "", tool_calls: [], note: NOTE };
  assert.equal(canShowNote(turn), true);
  assert.equal(noteText(turn), NOTE);
});

test("a step that ended in a tool call never shows the note", () => {
  // This is the reported bug: empty content here is normal, not a failed answer.
  const turn = { reasoning: "I should read the page.", content: "", tool_calls: [{ id: "c1", name: "wiki_read" }] };
  assert.equal(canShowNote(turn), false);
});

test("no note while anything is still streaming or a tool is running", () => {
  const turn = { reasoning: "Thinking.", content: "", tool_calls: [], note: NOTE };
  assert.equal(canShowNote(turn, { streaming: true }), false);
  assert.equal(canShowNote(turn, { runningTool: true }), false);
});

test("an answer without thinking has no note", () => {
  assert.equal(canShowNote({ reasoning: "", content: "The answer.", tool_calls: [] }), false);
});

test("thinking and an answer together is a normal answer", () => {
  const turn = { reasoning: "Thinking.", content: "The answer.", tool_calls: [] };
  assert.equal(canShowNote(turn), false);
});

test("the wording for a turn cut off by the token limit", () => {
  const turn = { reasoning: "Long thinking.", content: "", tool_calls: [], note: CUT_OFF };
  assert.equal(canShowNote(turn), true);
  assert.equal(noteText(turn), CUT_OFF);
});

test("a record without a stored note still says what happened", () => {
  // Transcripts written before the server stored the wording.
  assert.equal(noteText({ reasoning: "Thinking.", content: "", finish_reason: "stop" }), NOTE);
  assert.equal(noteText({ reasoning: "Thinking.", content: "", finish_reason: "length" }), CUT_OFF);
});
