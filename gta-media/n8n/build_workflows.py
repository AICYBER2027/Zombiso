"""Generate the ten n8n workflow exports in n8n/workflows/ (PLAN.md section 3.9).

Run:  python n8n/build_workflows.py

Every pipeline step is a POST to the VicePulse worker (``vicepulse serve``) at
http://worker:8088/run with body {"args": [<vicepulse CLI arguments>]}. The worker token is an
n8n "Header Auth" credential named "VicePulse worker" (Authorization: Bearer <token>); the
Telegram nodes use a credential named "VicePulse Telegram". Credential ids are placeholders:
after importing, open each node once and pick the credential.

Works on the free self-hosted n8n: no n8n Variables ($vars, a paid feature). Values that differ per
install are plain REPLACE_WITH_... placeholders, and the YouTube Data API key is an n8n "Query Auth"
credential named "YouTube Data API key" (query parameter name: key).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).parent / "workflows"
WORKER_URL = "http://worker:8088/run"
WORKER_CRED = {"httpHeaderAuth": {"id": "REPLACE_WITH_WORKER_CREDENTIAL_ID", "name": "VicePulse worker"}}
TELEGRAM_CRED = {"telegramApi": {"id": "REPLACE_WITH_TELEGRAM_CREDENTIAL_ID", "name": "VicePulse Telegram"}}
REVIEW_CHAT = "REPLACE_WITH_TELEGRAM_REVIEW_CHAT_ID"
N8N_URL = "http://127.0.0.1:5678"   # change if n8n is reached at another address
YOUTUBE_CRED = {"httpQueryAuth": {"id": "REPLACE_WITH_YOUTUBE_CREDENTIAL_ID", "name": "YouTube Data API key"}}


def _id(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vicepulse/{seed}"))


class Flow:
    def __init__(self, number: int, name: str, note: str):
        self.number, self.name, self.note = number, name, note
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}

    def node(self, name: str, type_: str, version: float, params: dict, *, creds: dict | None = None) -> str:
        n = {"parameters": params, "id": _id(f"{self.number}/{name}"), "name": name, "type": type_,
             "typeVersion": version, "position": [260 * len(self.nodes), 300]}
        if creds:
            n["credentials"] = creds
        self.nodes.append(n)
        return name

    def link(self, a: str, b: str, output: int = 0) -> None:
        outs = self.connections.setdefault(a, {"main": []})["main"]
        while len(outs) <= output:
            outs.append([])
        outs[output].append({"node": b, "type": "main", "index": 0})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    # -- building blocks
    def cron(self, name: str, expression: str) -> str:
        return self.node(name, "n8n-nodes-base.scheduleTrigger", 1.2,
                         {"rule": {"interval": [{"field": "cronExpression", "expression": expression}]}})

    def worker(self, name: str, args: list[str] | None = None, expr: str | None = None) -> str:
        """Call `vicepulse <args>` on the worker. ``expr`` is an n8n expression building the args array."""
        body = json.dumps({"args": args}) if args is not None else "={{ JSON.stringify({ args: " + expr + " }) }}"
        return self.node(name, "n8n-nodes-base.httpRequest", 4.2, {
            "method": "POST", "url": WORKER_URL, "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth", "sendBody": True, "specifyBody": "json", "jsonBody": body,
            "options": {"timeout": 3600000}}, creds=WORKER_CRED)

    def telegram(self, name: str, text: str, *, extra: dict | None = None) -> str:
        # Telegram nodes start disabled: until the owner creates the "VicePulse Telegram" credential,
        # an enabled Telegram node fails n8n's pre-run credential check and blocks the whole workflow.
        n = self.node(name, "n8n-nodes-base.telegram", 1.2,
                      {"chatId": REVIEW_CHAT, "text": text, "additionalFields": {"parse_mode": "HTML", **(extra or {})}},
                      creds=TELEGRAM_CRED)
        self.nodes[-1].update({"disabled": True, "notesInFlow": True,
                               "notes": "Disabled until the VicePulse Telegram credential and chat id are set"})
        return n

    def if_(self, name: str, left: str, op: str, right=None) -> str:
        cond = {"id": _id(f"{self.number}/{name}/c"), "leftValue": left,
                "operator": {"type": "boolean" if op in ("true", "false") else "string", "operation": op}}
        if right is not None:
            cond["rightValue"] = right
        return self.node(name, "n8n-nodes-base.if", 2.2, {"conditions": {
            "options": {"caseSensitive": True, "typeValidation": "loose"}, "conditions": [cond], "combinator": "and"}})

    def code(self, name: str, js: str) -> str:
        return self.node(name, "n8n-nodes-base.code", 2, {"jsCode": js})

    def export(self) -> dict:
        # No "tags": n8n's importer creates a shared tag once per file and the second file then
        # fails on the unique tag name, which rolls back the whole import.
        return {"name": f"VicePulse {self.number:02d} - {self.name}", "nodes": self.nodes,
                "connections": self.connections, "active": False,
                "settings": {"executionOrder": "v1", "timezone": "UTC", "saveManualExecutions": True},
                "pinData": {}, "meta": {"templateCredsSetupCompleted": False, "note": self.note}}


def build() -> list[Flow]:
    flows = []

    f = Flow(1, "Ingest and rights gate", "Called by the storage sync (or the creator upload form) for each new file.")
    t = f.node("New footage webhook", "n8n-nodes-base.webhook", 2,
               {"httpMethod": "POST", "path": "vicepulse/ingest", "authentication": "headerAuth",
                "responseMode": "lastNode", "options": {}}, creds={"httpHeaderAuth": WORKER_CRED["httpHeaderAuth"]})
    w = f.worker("Ingest + rights gate", expr='["ingest", "--file", $json.body.file, "--origin", $json.body.origin]'
                 '.concat($json.body.rights_id ? ["--rights-id", String($json.body.rights_id)] : [])')
    c = f.if_("Quarantined?", "={{ $json.status }}", "equals", "quarantined")
    a = f.telegram("Alert: quarantined", "=Footage quarantined (source {{ $json.source_id }}): {{ $json.reasons.join('; ') }}")
    f.chain(t, w, c)
    f.link(c, a, 0)
    flows.append(f)

    f = Flow(2, "Signal extraction", "Queue worker: analyses every cleared source. Heavy work runs on the worker container.")
    f.chain(f.cron("Every 10 minutes", "*/10 * * * *"), f.worker("Extract signals", ["signals"]))
    flows.append(f)

    f = Flow(3, "Select, script and render", "Daily run for VicePulse EN and VicePulse BR (5 clips each).")
    t = f.cron("Daily 05:15 UTC", "15 5 * * *")
    w = f.worker("Daily: select + script + render + check", ["daily"])
    s = f.telegram("Summary", "=Daily run done. Waiting for approval: {{ $json.check.pending_approval.length }}, "
                              "failed check: {{ $json.check.failed_check.length }}.")
    f.chain(t, w, s)
    flows.append(f)

    f = Flow(4, "Approval queue", "Sends each clip to the reviewer; the reviewer decides in an n8n form. TikTok requires "
                                  "the person to choose privacy and interaction settings for every post, with no defaults.")
    t = f.cron("Daily 06:30 UTC", "30 6 * * *")
    q = f.worker("Approval queue", ["queue"])
    split = f.code("One item per clip", "return $json.pending.map(c => ({ json: c }));")
    msg = f.telegram("Ask reviewer", "=<b>Clip {{ $json.id }}</b> ({{ $json.channel }}, {{ $json.for_date }})\n"
                                    "{{ $json.hook }}\nFiles: {{ $json.files.join(', ') }}\n"
                                    "Decide here: " + N8N_URL + "/form/vicepulse-approve?clip={{ $json.id }}")
    f.chain(t, q, split, msg)
    form = f.node("Approval form", "n8n-nodes-base.formTrigger", 2.2, {
        "path": "vicepulse-approve", "formTitle": "VicePulse clip approval",
        "formDescription": "Watch all three renders before deciding. Nothing here is pre-selected on purpose.",
        "formFields": {"values": [
            {"fieldLabel": "Clip id", "fieldType": "number", "requiredField": True},
            {"fieldLabel": "Reviewer", "requiredField": True},
            {"fieldLabel": "Decision", "fieldType": "dropdown", "requiredField": True,
             "fieldOptions": {"values": [{"option": "approve"}, {"option": "reject"}]}},
            {"fieldLabel": "Reason (if rejecting)"},
            {"fieldLabel": "TikTok privacy level", "fieldType": "dropdown", "requiredField": True,
             "fieldOptions": {"values": [{"option": "SELF_ONLY"}, {"option": "MUTUAL_FOLLOW_FRIENDS"},
                                         {"option": "FOLLOWER_OF_CREATOR"}, {"option": "PUBLIC_TO_EVERYONE"}]}},
            {"fieldLabel": "TikTok interactions", "fieldType": "checkbox",
             "fieldOptions": {"values": [{"option": "Allow comments"}, {"option": "Allow Duet"}, {"option": "Allow Stitch"}]}},
            {"fieldLabel": "TikTok commercial content", "fieldType": "checkbox",
             "fieldOptions": {"values": [{"option": "Your brand"}, {"option": "Branded content"}]}},
            {"fieldLabel": "Consent", "fieldType": "checkbox", "requiredField": True,
             "fieldOptions": {"values": [{"option": "I agree to TikTok's Music Usage Confirmation"}]}},
        ]}, "options": {}})
    build_args = f.code("Build decision", """
const j = $json;
const ticks = j['TikTok interactions'] || [];
const commercial = j['TikTok commercial content'] || [];
const tiktok = {
  privacy_level: j['TikTok privacy level'],
  allow_comment: ticks.includes('Allow comments'),
  allow_duet: ticks.includes('Allow Duet'),
  allow_stitch: ticks.includes('Allow Stitch'),
  your_brand: commercial.includes('Your brand'),
  branded_content: commercial.includes('Branded content'),
  consent_confirmed: (j['Consent'] || []).length === 1,
};
if (tiktok.branded_content && tiktok.privacy_level === 'SELF_ONLY') {
  throw new Error('Branded content cannot be private on TikTok. Pick another privacy level.');
}
const args = ['decide', '--clip', String(j['Clip id']), '--decision', j['Decision'], '--by', j['Reviewer'],
              '--tiktok-options', JSON.stringify(tiktok)];
if (j['Reason (if rejecting)']) args.push('--reason', j['Reason (if rejecting)']);
return [{ json: { args } }];""".strip())
    d = f.worker("Record decision", expr="$json.args")
    f.chain(form, build_args, d)
    flows.append(f)

    f = Flow(5, "Publisher", "Every 15 minutes: schedule approved clips, then post what is due. Dry run unless "
                             "VICEPULSE_ALLOW_LIVE_PUBLISH=1 and the account row is enabled.")
    t = f.cron("Every 15 minutes", "*/15 * * * *")
    s = f.worker("Schedule approved clips", ["schedule"])
    p = f.worker("Publish due posts", ["publish"])
    c = f.if_("Anything failed or blocked?", "={{ $json.failed.length + $json.blocked.length > 0 }}", "true")
    a = f.telegram("Alert: publish problem", "=Publish problems. Failed: {{ $json.failed.join(', ') }}. "
                                            "Blocked: {{ $json.blocked.join(', ') }}. Run the health check for details.")
    f.chain(t, s, p, c)
    f.link(c, a, 0)
    flows.append(f)

    f = Flow(6, "Analytics pull", "Nightly metrics for everything published in the last 30 days.")
    f.chain(f.cron("Nightly 02:30 UTC", "30 2 * * *"), f.worker("Pull analytics", ["analytics"]))
    flows.append(f)

    f = Flow(7, "Trend and topic scan", "Every 4 hours. Reads public feeds only; produces topics, never footage. "
                                        "Fill in the Rockstar feed URL placeholder and attach the YouTube Data API key credential.")
    t = f.cron("Every 4 hours", "0 */4 * * *")
    rss = f.node("Rockstar Newswire feed", "n8n-nodes-base.rssFeedRead", 1.1,
                 {"url": "REPLACE_WITH_ROCKSTAR_NEWSWIRE_FEED_URL", "options": {}})
    yt_en = f.node("YouTube trending gaming (US)", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "GET", "url": "https://www.googleapis.com/youtube/v3/videos", "sendQuery": True,
        "queryParameters": {"parameters": [
            {"name": "part", "value": "snippet"}, {"name": "chart", "value": "mostPopular"},
            {"name": "videoCategoryId", "value": "20"}, {"name": "regionCode", "value": "US"},
            {"name": "maxResults", "value": "25"}]},
        "authentication": "genericCredentialType", "genericAuthType": "httpQueryAuth",
        "options": {}}, creds=YOUTUBE_CRED)
    yt_br = f.node("YouTube trending gaming (BR)", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "GET", "url": "https://www.googleapis.com/youtube/v3/videos", "sendQuery": True,
        "queryParameters": {"parameters": [
            {"name": "part", "value": "snippet"}, {"name": "chart", "value": "mostPopular"},
            {"name": "videoCategoryId", "value": "20"}, {"name": "regionCode", "value": "BR"},
            {"name": "maxResults", "value": "25"}]},
        "authentication": "genericCredentialType", "genericAuthType": "httpQueryAuth",
        "options": {}}, creds=YOUTUBE_CRED)
    to_topics = f.code("To topics", """
const out = [];
for (const item of $input.all()) {
  const j = item.json;
  if (j.title && j.link) out.push({ topic: j.title, source: 'rockstar_newswire', score: 5, language: 'en' });
  for (const v of (j.items || [])) {
    const t = v.snippet.title;
    if (!/gta|grand theft auto/i.test(t)) continue;
    const br = (v.snippet.defaultAudioLanguage || '').startsWith('pt');
    out.push({ topic: t, source: 'youtube_trending_gaming', score: 3, language: br ? 'pt-BR' : 'en' });
  }
}
return [{ json: { args: ['topics-add', '--json', JSON.stringify(out)] } }];""".strip())
    store = f.worker("Store topics", expr="$json.args")
    merge = f.node("Merge", "n8n-nodes-base.merge", 3, {"numberInputs": 3})
    for i, n in enumerate((rss, yt_en, yt_br)):
        f.link(t, n)
        f.connections.setdefault(n, {"main": [[]]})["main"][0].append({"node": merge, "type": "main", "index": i})
    f.chain(merge, to_topics, store)
    flows.append(f)

    f = Flow(8, "Creator prospecting", "Weekly. Finds GTA creators to contact for a written licence. It stores names "
                                       "and channel ids only; it never downloads anyone's videos.")
    t = f.cron("Mondays 09:00 UTC", "0 9 * * 1")
    searches = []
    for lang, q in (("en", "GTA 6 gameplay"), ("pt", "GTA 6 gameplay português")):
        searches.append(f.node(f"YouTube channel search ({lang})", "n8n-nodes-base.httpRequest", 4.2, {
            "method": "GET", "url": "https://www.googleapis.com/youtube/v3/search", "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "part", "value": "snippet"}, {"name": "type", "value": "channel"},
                {"name": "q", "value": q}, {"name": "relevanceLanguage", "value": lang},
                {"name": "maxResults", "value": "25"}]},
            "authentication": "genericCredentialType", "genericAuthType": "httpQueryAuth",
            "options": {}}, creds=YOUTUBE_CRED))
    merge = f.node("Merge", "n8n-nodes-base.merge", 3, {"numberInputs": 2})
    to_p = f.code("To prospects", """
const out = [];
for (const item of $input.all()) {
  for (const r of (item.json.items || [])) {
    out.push({ platform: 'youtube', channel_ref: r.snippet.channelId, display_name: r.snippet.channelTitle,
               language: /portugu/i.test(r.snippet.description || '') ? 'pt-BR' : 'en' });
  }
}
return [{ json: { args: ['prospects-add', '--json', JSON.stringify(out)] } }];""".strip())
    store = f.worker("Store prospects", expr="$json.args")
    for i, n in enumerate(searches):
        f.link(t, n)
        f.connections.setdefault(n, {"main": [[]]})["main"][0].append({"node": merge, "type": "main", "index": i})
    f.chain(merge, to_p, store)
    flows.append(f)

    f = Flow(9, "Takedown", "Manual trigger. Removes a clip (or everything from a source) everywhere the API allows "
                            "and lists the posts a person must delete by hand (Instagram and TikTok have no delete API).")
    form = f.node("Takedown form", "n8n-nodes-base.formTrigger", 2.2, {
        "path": "vicepulse-takedown", "formTitle": "VicePulse takedown",
        "formFields": {"values": [
            {"fieldLabel": "What", "fieldType": "dropdown", "requiredField": True,
             "fieldOptions": {"values": [{"option": "clip"}, {"option": "source"}]}},
            {"fieldLabel": "Id", "fieldType": "number", "requiredField": True},
            {"fieldLabel": "Reason", "requiredField": True}]}, "options": {}})
    w = f.worker("Take down", expr='["takedown", "--" + $json["What"], String($json["Id"]), "--reason", $json["Reason"]]')
    a = f.telegram("Manual removals needed", "=Takedown done. Removed via API: {{ $json.removed.length }}. "
                                            "Remove by hand: {{ $json.manual_removal_needed.map(m => m.platform + ' ' + "
                                            "m.channel + ' ' + (m.url || m.external_id)).join('; ') || 'none' }}")
    f.chain(form, w, a)
    flows.append(f)

    f = Flow(10, "Health check and alerts", "Hourly: failed or blocked posts, expiring licences, short days, waiting approvals.")
    t = f.cron("Hourly", "5 * * * *")
    # Lets `n8n execute --id <id>` run this workflow from a terminal (a schedule trigger cannot start a CLI run).
    cli = f.node("Run from command line", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"})
    w = f.worker("Health check", ["health"])
    f.link(cli, w)
    c = f.if_("Any alerts?", "={{ $json.healthy }}", "false")
    a = f.telegram("Alert", "=VicePulse health:\n{{ $json.alerts.join('\\n') }}")
    f.chain(t, w, c)
    f.link(c, a, 0)
    flows.append(f)
    return flows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in build():
        slug = f.name.lower().replace(",", "").replace(" ", "-")
        (OUT / f"{f.number:02d}-{slug}.json").write_text(json.dumps(f.export(), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
