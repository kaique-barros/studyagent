"""Cliente mínimo do Groq (API compatível com OpenAI) com orçamento diário local.

Limites do plano gratuito (~30 req/min, ~1000 req/dia, 8K tokens/min, 200K tokens/dia) são respeitados com folga:
o orçamento é contado num arquivo e a chamada é recusada antes de estourar.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"      # troque com STUDYAGENT_GROQ_MODEL se o Groq aposentar este


class LLMError(Exception):
    pass


class Budget:
    def __init__(self, path: Path, max_requests: int = 800, max_tokens: int = 150_000, today=lambda: date.today().isoformat()):
        self.path, self.max_requests, self.max_tokens, self.today = path, max_requests, max_tokens, today

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        return d if d.get("day") == self.today() else {"day": self.today(), "requests": 0, "tokens": 0}

    def check(self, est_tokens: int) -> None:
        d = self._load()
        if d["requests"] + 1 > self.max_requests or d["tokens"] + est_tokens > self.max_tokens:
            raise LLMError("orçamento diário do Groq esgotado (volta amanhã)")

    def add(self, tokens: int) -> None:
        d = self._load()
        d["requests"] += 1
        d["tokens"] += tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(self.path)


class GroqClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, budget: Budget | None = None,
                 client: httpx.Client | None = None, sleep=time.sleep):
        if not api_key:
            raise LLMError("GROQ_API_KEY não definido")
        self.model, self.budget, self.sleep = model, budget, sleep
        self.http = client or httpx.Client(base_url=BASE_URL, timeout=60)
        self.api_key = api_key

    def chat_json(self, system: str, user: str, max_tokens: int = 1000) -> dict[str, Any]:
        est = (len(system) + len(user)) // 3 + max_tokens
        if est > 7000:                                   # 8K tokens por minuto no plano gratuito
            raise LLMError(f"prompt grande demais (~{est} tokens)")
        if self.budget:
            self.budget.check(est)
        body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        for attempt in range(3):
            try:
                r = self.http.post("/chat/completions", json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.TransportError as e:
                raise LLMError(f"falha de rede falando com o Groq: {e}") from e
            if r.status_code == 429 and attempt < 2:
                self.sleep(min(float(r.headers.get("retry-after", "5") or 5), 30))
                continue
            break
        if r.status_code >= 400:
            raise LLMError(f"Groq respondeu {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            out = json.loads(content)
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"resposta do Groq não é JSON válido: {e}") from e
        if self.budget:
            self.budget.add(int(data.get("usage", {}).get("total_tokens") or est))
        if not isinstance(out, dict):
            raise LLMError("resposta do Groq não é um objeto JSON")
        return out


def from_env(state_dir: Path) -> GroqClient:
    return GroqClient(os.environ.get("GROQ_API_KEY", ""), os.environ.get("STUDYAGENT_GROQ_MODEL", DEFAULT_MODEL),
                      Budget(state_dir / "groq-budget.json"))
