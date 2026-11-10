"""Claude calls for triage, selection, commentary and the final policy check.

Two implementations share one interface:
- ``ClaudeLLM`` calls the Claude API with the official ``anthropic`` SDK (billed; only used
  when LLM_MODE=anthropic).
- ``MockLLM`` is deterministic and offline. Tests and dry runs use it.

Every answer is structured JSON enforced with ``output_config.format`` so n8n and the
pipeline can act on it directly.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Protocol

from .config import Settings

# ---------------------------------------------------------------- JSON schemas

POLICY_TAGS = ["story_spoiler", "ending", "cutscene_only", "leak", "pre_release", "mission_reveal",
               "in_game_radio_music", "graphic_violence_focus", "personal_data_on_screen", "other_creator_watermark"]

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "hook_score": {"type": "number", "description": "0-10: would the first 2 seconds stop a scroll?"},
        "clarity_score": {"type": "number", "description": "0-10: understandable with no context?"},
        "policy_tags": {"type": "array", "items": {"type": "string", "enum": POLICY_TAGS}},
        "summary": {"type": "string"},
    },
    "required": ["hook_score", "clarity_score", "policy_tags", "summary"],
    "additionalProperties": False,
}

SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "in_s": {"type": "number"},
                    "out_s": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["candidate_id", "in_s", "out_s", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}

_PLATFORM_COPY = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "hashtags"],
    "additionalProperties": False,
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string", "description": "Spoken/on-screen line for the first 2 seconds"},
        "body": {"type": "string", "description": "An actual opinion or piece of context"},
        "closer": {"type": "string"},
        "angle": {"type": "string", "description": "Short label of the joke/angle, used for repetition checks"},
        "youtube": _PLATFORM_COPY,
        "instagram": _PLATFORM_COPY,
        "tiktok": _PLATFORM_COPY,
    },
    "required": ["hook", "body", "closer", "angle", "youtube", "instagram", "tiktok"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "policy_tags": {"type": "array", "items": {"type": "string", "enum": POLICY_TAGS}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "quality_score": {"type": "number"},
    },
    "required": ["pass", "policy_tags", "issues", "quality_score"],
    "additionalProperties": False,
}


class LLM(Protocol):
    def triage(self, candidate: dict, frames: list[Path], *, game: str) -> dict: ...
    def select(self, channel: dict, candidates: list[dict], n: int, *, examples: list[dict],
               topics: list[str], game: str) -> dict: ...
    def script(self, channel: dict, clip: dict, frames: list[Path], *, game: str) -> dict: ...
    def final_check(self, channel: dict, clip: dict, frames: list[Path], *, game: str,
                    block_tags: list[str]) -> dict: ...


class LLMRefusal(RuntimeError):
    pass


def _image_block(path: Path) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(Path(path).read_bytes()).decode()}}


SYSTEM_PROMPT = (
    "You work for VicePulse, a short-form gaming channel about Grand Theft Auto. You see sampled frames and "
    "signal data from gameplay footage the channel owns or has licensed in writing. You never see the video "
    "itself. Judge what would make an original, worthwhile 15-45 second vertical clip with real added commentary. "
    "Flag anything that would break the publisher's video policy or a platform rule: story spoilers, endings, "
    "cutscene-only material, leaks or pre-release material, licensed in-game radio music, other creators' "
    "watermarks, personal data on screen. When unsure, flag it; a person reviews every flag."
)


class ClaudeLLM:
    """Real Claude API client. Every call here is billed."""

    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def _call(self, *, model: str, effort: str, schema: dict, content: list[dict], max_tokens: int = 16000) -> dict:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": content}],
        }
        # Server-side refusal fallback is offered for the Opus-tier model, not for Haiku.
        if self.settings.llm_fallbacks and model == self.settings.model_main:
            response = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            response = self.client.messages.create(**params)
        if response.stop_reason == "refusal":
            raise LLMRefusal(f"model declined: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("model output hit max_tokens before finishing the JSON answer")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def triage(self, candidate, frames, *, game):
        content = [_image_block(f) for f in frames] + [{"type": "text", "text": (
            f"Game: {game}. Candidate window {candidate['start_s']:.1f}s-{candidate['end_s']:.1f}s, "
            f"signal score {candidate['signal_score']}. Transcript: {candidate.get('transcript') or '(none)'}\n"
            "Score this window as a short-form highlight and list any policy tags.")}]
        return self._call(model=self.settings.model_triage, effort="low", schema=TRIAGE_SCHEMA, content=content,
                          max_tokens=4000)

    def select(self, channel, candidates, n, *, examples, topics, game):
        content: list[dict] = []
        for c in candidates:
            content.extend(_image_block(f) for f in c.get("frames", []))
            content.append({"type": "text", "text": json.dumps(
                {k: c[k] for k in ("id", "start_s", "end_s", "summary", "triage_score", "transcript") if k in c})})
        content.append({"type": "text", "text": (
            f"Channel: {channel['name']} ({channel['language']}). Game: {game}. Choose exactly {n} highlights "
            f"from the candidates above (fewer only if fewer are usable). Set in/out points inside each "
            f"candidate's window, 15-45 seconds where possible, starting on the action. Picks must not overlap "
            f"each other in time or show the same moment twice. Explain each pick.\n"
            f"Today's topics: {json.dumps(topics)}\nRecent best and worst performers: {json.dumps(examples)}")})
        return self._call(model=self.settings.model_main, effort="high", schema=SELECT_SCHEMA, content=content)

    def script(self, channel, clip, frames, *, game):
        content = [_image_block(f) for f in frames] + [{"type": "text", "text": (
            f"Write the commentary for one {clip['out_s'] - clip['in_s']:.0f}-second clip for {channel['name']}. "
            f"Language: {channel['language']} (write natively in that language, never as a translation). "
            f"Voice: {channel['voice_style']}\nGame: {game}. Why it was picked: {clip.get('selection_reason')}\n"
            "Give a hook for the first two seconds, a real opinion or piece of context, and a closing line. "
            "Then write a title, description and hashtags separately for YouTube Shorts, Instagram Reels and TikTok. "
            f"Use these brand hashtags where they fit: {' '.join(channel.get('hashtags', []))}. "
            "No spoilers, no claims you cannot see in the frames, no other creators' names unless credited.")}]
        return self._call(model=self.settings.model_main, effort="high", schema=SCRIPT_SCHEMA, content=content)

    def final_check(self, channel, clip, frames, *, game, block_tags):
        content = [_image_block(f) for f in frames] + [{"type": "text", "text": (
            f"Final check before a person approves this {channel['language']} clip for {channel['name']}. "
            f"Script and titles: {json.dumps(clip.get('script'), ensure_ascii=False)}\n"
            f"Blocked tags for {game}: {block_tags}. Fail it if any blocked tag applies, if the commentary adds "
            "no real value, if the captions are wrong for the language, or if titles promise something the clip "
            "does not show.")}]
        return self._call(model=self.settings.model_main, effort="high", schema=CHECK_SCHEMA, content=content)


# ---------------------------------------------------------------- offline mock

_HOOKS = {
    "en": ["Nobody expected this to work.", "This is why the police AI is broken.", "Watch the timing here.",
           "Rate this escape from 1 to 10.", "The physics engine had other plans.", "This should not be possible.",
           "One wrong turn changed everything.", "Pure chaos in under ten seconds."],
    "pt-BR": ["Ninguem esperava que isso desse certo.", "Por isso a policia do jogo e quebrada.",
              "Repara no timing aqui.", "Da uma nota de 1 a 10 pra essa fuga.", "A fisica do jogo tinha outros planos.",
              "Isso nao devia ser possivel.", "Uma curva errada mudou tudo.", "Caos total em menos de dez segundos."],
}
_BODIES = {
    "en": ["The wanted level jumps the moment the car clips the barrier, and that is the whole lesson.",
           "Most players brake here. Keeping the throttle open is what makes the jump land.",
           "You can see the traffic AI give up a full second before the crash.",
           "The camera cut hides how close that was to failing."],
    "pt-BR": ["O nivel de procurado sobe na hora que o carro raspa na barreira, e essa e a licao.",
              "A maioria freia aqui. Manter o acelerador e o que faz o salto dar certo.",
              "Da pra ver a IA do transito desistindo um segundo antes da batida.",
              "O corte de camera esconde o quanto isso quase deu errado."],
}


class MockLLM:
    """Deterministic, offline stand-in for Claude. Records every call for tests."""

    def __init__(self, flag_candidates: dict[int, list[str]] | None = None, fail_check_clips: set[int] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.flag_candidates = flag_candidates or {}
        self.fail_check_clips = fail_check_clips or set()

    def triage(self, candidate, frames, *, game):
        self.calls.append(("triage", {"candidate_id": candidate["id"], "frames": len(frames)}))
        score = min(10.0, 4.0 + float(candidate.get("signal_score") or 0))
        return {"hook_score": score, "clarity_score": 7.0,
                "policy_tags": self.flag_candidates.get(candidate["id"], []),
                "summary": f"mock summary for window {candidate['start_s']:.0f}-{candidate['end_s']:.0f}s"}

    def select(self, channel, candidates, n, *, examples, topics, game):
        self.calls.append(("select", {"channel": channel["key"], "n": n, "ids": [c["id"] for c in candidates]}))
        picks = []
        for c in sorted(candidates, key=lambda c: (-(c.get("triage_score") or 0), c["id"])):
            if len(picks) == n:
                break
            if any(min(c["end_s"], p["out_s"]) - max(c["start_s"], p["in_s"]) > 0 for p in picks):
                continue  # like the real prompt: picks must not overlap each other
            picks.append({"candidate_id": c["id"], "in_s": c["start_s"], "out_s": c["end_s"],
                          "reason": "highest combined hook and clarity score (mock)"})
        return {"picks": picks}

    def script(self, channel, clip, frames, *, game):
        lang = channel["language"] if channel["language"] in _HOOKS else "en"
        self.calls.append(("script", {"clip_id": clip["id"], "language": lang}))
        i = clip["id"]
        hook, body = _HOOKS[lang][i % len(_HOOKS[lang])], _BODIES[lang][i % len(_BODIES[lang])]
        closer = "Follow for more." if lang == "en" else "Segue pra mais."
        tags = channel.get("hashtags", [])
        copy = {"title": hook, "description": body, "hashtags": tags}
        return {"hook": hook, "body": body, "closer": closer, "angle": f"mock-angle-{i}",
                "youtube": dict(copy, title=f"{hook} #Shorts"), "instagram": copy, "tiktok": copy}

    def final_check(self, channel, clip, frames, *, game, block_tags):
        self.calls.append(("final_check", {"clip_id": clip["id"]}))
        if clip["id"] in self.fail_check_clips:
            return {"pass": False, "policy_tags": ["story_spoiler"], "issues": ["mock: spoiler visible"], "quality_score": 3}
        return {"pass": True, "policy_tags": [], "issues": [], "quality_score": 8}


def make_llm(settings: Settings) -> LLM:
    if settings.llm_mode == "anthropic":
        return ClaudeLLM(settings)
    if settings.llm_mode == "mock":
        return MockLLM()
    raise ValueError(f"unknown LLM_MODE {settings.llm_mode!r}")
