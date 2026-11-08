// When a missing answer is real. The wording comes from the server (agent.py), so this module only
// answers the state question: may a note be shown for this turn at all.
//
// - Never for a step that ended in a tool call. Thinking then a tool call is a normal intermediate
//   step: the model has no answer text yet, and the answer comes after the tool result.
// - Never while anything is still streaming or a tool is still running.
// - Only for the final step of a turn: thinking, no answer text, and the stream for that step ended.

export function canShowNote(turn, { streaming = false, runningTool = false } = {}) {
  if (!turn || streaming || runningTool) return false;
  if (turn.tool_calls && turn.tool_calls.length) return false;
  return Boolean(turn.reasoning) && !turn.content;
}

export function noteText(record) {
  if (record.note) return record.note;
  // Records written before the server stored the wording. The same reading, best we can tell.
  if (record.finish_reason === "length") {
    return "The response was cut off by the token limit during reasoning.";
  }
  return "The model produced reasoning but no final answer.";
}
