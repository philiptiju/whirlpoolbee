import json
from html.parser import HTMLParser
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest


PAYLOAD = """"><img src=x onerror="window.injected=1"><svg onload='window.injected=1'>"""


class ParsedHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def run_renderer(api_app, template, function, data):
    if not shutil.which("node"):
        pytest.skip("Node.js is needed to execute template JavaScript")
    request = SimpleNamespace(
        state=SimpleNamespace(root_path="", is_admin=True, is_super_user=False),
        url=SimpleNamespace(path="/"), session={},
    )
    html = api_app.state.templates.get_template(template + ".html").render(
        request=request, machine_id="mine", selected_machine=None, sync_log=[]
    )
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    helper = next(re.search(r"function escapeHtml\(str\) \{.*?\n        \}", s, re.S).group()
                  for s in scripts if "function escapeHtml(str)" in s and "const IS_ADMIN" in s)
    script = next(s for s in scripts if f"function {function}(" in s)
    # Exercise individual renderers without launching unrelated initial API calls.
    script = re.sub(r"\nloadAll\(\);\s*$", "", script)
    if function != "refreshSyncLog":
        export = f"\nglobalThis.testRenderer = {function};\n"
        if script.rstrip().endswith("})();"):
            position = script.rfind("})();")
            script = script[:position] + export + script[position:]
        else:
            script += export
    runner = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const timers = [];
const context = vm.createContext({
  ROOT_PATH: '', URLSearchParams, Date, Math, JSON,
  window: {location: {search: '?machine_id=mine'}},
  localStorage: {getItem: () => null},
  console: {error: (...args) => { throw Error(args.join(' ')); }},
  setInterval: callback => timers.push(callback),
  formatLocalDateTime: value => value,
  Chart: function() { this.destroy = () => {}; },
  fetch: async () => ({ok: true, json: async () => input.data}),
  document: {
    addEventListener() {},
    querySelectorAll: () => [],
    getElementById: id => elements[id] ||= {
      innerHTML: '', textContent: '', value: '30', style: {},
      classList: {add() {}, remove() {}}, getContext: () => ({})
    },
    createElement: () => ({
      textContent: '',
      get innerHTML() {
        return String(this.textContent).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      }
    })
  },
  data: input.data
});
vm.runInContext(input.helper + '\n' + input.script, context);
(async () => {
  if (input.function === 'refreshSyncLog') await timers[0]();
  else await vm.runInContext('testRenderer(data)', context);
  process.stdout.write(JSON.stringify(elements));
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        ["node", "-e", runner], input=json.dumps({
            "helper": helper, "script": script, "function": function, "data": data
        }), text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


CASES = [
    ("analytics", "displaySuggestions", "suggestionsList", {
        "suggestion_type": PAYLOAD, "suggestion_text": PAYLOAD,
        "code_before": PAYLOAD, "code_after": PAYLOAD, "was_accepted": True,
    }),
    ("analytics", "displayRefactoringImpact", "refactoringList", {"file_path": PAYLOAD}),
    ("analytics", "loadFileHeatmap", "heatmapTable", {
        "file_path": PAYLOAD, "language": PAYLOAD, "modification_count": 1,
    }),
    ("learning", "displayPractices", "practicesList", {
        "title": PAYLOAD, "description": PAYLOAD, "category": PAYLOAD,
        "difficulty_level": PAYLOAD, "example_prompt": PAYLOAD,
        "example_code": PAYLOAD, "tags": json.dumps([PAYLOAD]),
    }),
    ("learning", "displayPatterns", "patternsList", {
        "title": PAYLOAD, "description": PAYLOAD, "pattern_type": PAYLOAD,
        "language": PAYLOAD, "use_case": PAYLOAD, "content": PAYLOAD,
        "tags": json.dumps([PAYLOAD]),
    }),
    ("optimization", "displayPrompts", "promptsList", {
        "session_id": "session", "prompt_text": PAYLOAD,
        "suggestions": PAYLOAD, "improved_prompt": PAYLOAD,
    }),
    ("optimization", "displayContextInsights", "contextList", {
        "session_id": "session", "recommendations": PAYLOAD,
    }),
    ("optimization", "displayModels", "modelsList", {"model_name": PAYLOAD, "task_type": PAYLOAD}),
    ("optimization", "displayWastePatterns", "wasteList", {
        "pattern_type": PAYLOAD, "description": PAYLOAD, "recommendation": PAYLOAD,
    }),
]


@pytest.mark.parametrize("template,function,container,record", CASES)
def test_dynamic_content_is_text_not_markup(api_app, template, function, container, record):
    elements = run_renderer(api_app, template, function, [record])
    parsed = ParsedHTML(elements[container]["innerHTML"])
    visible_values = parsed.text + [value for _, attrs in parsed.tags for value in attrs.values() if value]
    assert any("<img" in text for text in visible_values)
    assert not any(tag in ("img", "svg", "script") for tag, _ in parsed.tags)
    assert not any(key.startswith("on") for _, attrs in parsed.tags for key in attrs)


def test_insights_escapes_prompt_text_and_projects(api_app):
    elements = run_renderer(api_app, "insights", "loadTopPrompts", {
        "top_words": [{"word": PAYLOAD, "count": 1}],
        "prompts": [{"project": PAYLOAD, "text": PAYLOAD}],
    })
    for container in ["topPromptsWords", "topPromptsList"]:
        parsed = ParsedHTML(elements[container]["innerHTML"])
        assert not any(tag in ("img", "svg", "script") for tag, _ in parsed.tags)
        assert any("<img" in text for text in parsed.text)


def test_sync_refresh_keeps_all_seven_columns(api_app):
    result = run_renderer(api_app, "sync", "refreshSyncLog", [{
        "synced_at": "2026-10-01T00:00:00Z",
        "logged_in_user": "Example User <user@example.com>",
        "machine_name": "Workstation",
        "source": "jsonl", "items_synced": 2, "items_total": 3,
        "message": PAYLOAD,
    }])
    html = result["syncLogBody"]["innerHTML"]
    cells = re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)
    assert len(cells) == 7
    assert "Example User" in cells[1]
    assert "Workstation" in cells[2]
    assert "jsonl" in cells[3]
    assert "2 / 3" in cells[4]
    assert not any(tag == "img" for tag, _ in ParsedHTML(cells[6]).tags)
    empty = run_renderer(api_app, "sync", "refreshSyncLog", [])
    assert 'colspan="7"' in empty["syncLogBody"]["innerHTML"]
