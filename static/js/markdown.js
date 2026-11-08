// Small markdown renderer plus a small highlighter. No dependencies, no CDN.
import { escapeHtml } from "./api.js";

const KEYWORDS = {
  python: "def|return|if|elif|else|for|while|import|from|as|class|try|except|with|lambda|None|True|False|and|or|not|in|print|async|await",
  javascript: "const|let|var|function|return|if|else|for|while|class|new|await|async|import|export|from|null|true|false|this",
  bash: "if|then|else|fi|for|do|done|case|esac|echo|export|local|function|set|cd",
  sql: "select|from|where|group|order|by|insert|update|delete|create|table|join|on|and|or|null|as",
  shell: "if|then|else|fi|for|do|done|echo|export|cd",
};

const LANGUAGE_ALIASES = { py: "python", sh: "bash", zsh: "bash", js: "javascript", ts: "javascript" };

function highlight(code, language) {
  const key = LANGUAGE_ALIASES[(language || "").toLowerCase()] || (language || "").toLowerCase();
  const keywords = KEYWORDS[key];
  const parts = [
    "(?<com>//[^\\n]*|#[^\\n]*|--[^\\n]*)",
    "(?<str>\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')",
    "(?<num>\\b\\d+(?:\\.\\d+)?\\b)",
  ];
  if (keywords) parts.push(`(?<key>\\b(?:${keywords})\\b)`);

  let out = "";
  let last = 0;
  for (const match of code.matchAll(new RegExp(parts.join("|"), "g"))) {
    out += escapeHtml(code.slice(last, match.index));
    const kind = Object.keys(match.groups).find((name) => match.groups[name]);
    out += `<span class="tok-${kind}">${escapeHtml(match[0])}</span>`;
    last = match.index + match[0].length;
  }
  return out + escapeHtml(code.slice(last));
}

function inline(text, options) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, (_match, code) => `<code class="inline">${code}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, (_match, before, word) => `${before}<em>${word}</em>`);
  if (options.wikiLinks) {
    out = out.replace(/\[\[([a-z0-9][a-z0-9_-]*)\]\]/g, (_match, name) =>
      `<a class="wikilink" href="/wiki?p=${name}">${name}</a>`);
  }
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>');
  return out;
}

function codeBlock(code, language) {
  return `<pre class="code"><span class="code-head"><span class="lang">${escapeHtml(language || "text")}</span>` +
    `<button class="copy" type="button" data-copy title="Copy code">Copy</button></span>` +
    `<code>${highlight(code.replace(/\n$/, ""), language)}</code></pre>`;
}

export function renderMarkdown(source, options = {}) {
  const lines = String(source).split("\n");
  const out = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (line.startsWith("```")) {
      const language = line.slice(3).trim();
      const body = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) body.push(lines[index++]);
      index += 1;
      out.push(codeBlock(body.join("\n"), language));
      continue;
    }

    if (!line.trim()) { index++; continue; }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { out.push(`<h${heading[1].length}>${inline(heading[2], options)}</h${heading[1].length}>`); index++; continue; }

    if (/^(-{3,}|\*\*\*)$/.test(line.trim())) { out.push("<hr>"); index++; continue; }

    if (line.startsWith("> ")) {
      const body = [];
      while (index < lines.length && lines[index].startsWith("> ")) body.push(lines[index++].slice(2));
      out.push(`<blockquote>${inline(body.join(" "), options)}</blockquote>`);
      continue;
    }

    if (line.startsWith("|") && index + 1 < lines.length && /^\|[-\s|:]+\|$/.test(lines[index + 1].trim())) {
      const rows = [];
      while (index < lines.length && lines[index].startsWith("|")) rows.push(lines[index++].trim());
      const cells = rows.filter((row) => !/^\|[-\s|:]+\|$/.test(row))
        .map((row) => row.split("|").slice(1, -1).map((cell) => inline(cell.trim(), options)));
      const table = `<div class="table-wrap"><table><thead><tr>${cells[0].map((cell) => `<th>${cell}</th>`).join("")}</tr></thead>` +
        `<tbody>${cells.slice(1).map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
      out.push(table);
      continue;
    }

    if (line.match(/^([-*]|\d+\.)\s+/)) {
      const ordered = /\d+\./.test(line.trim());
      const items = [];
      while (index < lines.length && lines[index].match(/^([-*]|\d+\.)\s+/)) {
        items.push(`<li>${inline(lines[index++].trim().replace(/^([-*]|\d+\.)\s+/, ""), options)}</li>`);
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !lines[index].startsWith("```") &&
           !lines[index].match(/^(#{1,4})\s/) && !lines[index].match(/^([-*]|\d+\.)\s/) &&
           !lines[index].startsWith("> ") && !lines[index].startsWith("|")) {
      paragraph.push(lines[index++]);
    }
    out.push(`<p>${inline(paragraph.join(" "), options)}</p>`);
  }

  return out.join("");
}

export { highlight };
