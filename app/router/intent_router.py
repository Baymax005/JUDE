from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from app.config import (
    TIER1_CONFIDENCE_THRESHOLD,
    TIER1_CONFIRM_THRESHOLD,
    TIER1_MODEL_DIR,
)


@dataclass(frozen=True)
class RouteResult:
    """Structured prediction returned by the Tier 1 ONNX intent router."""

    text: str
    intent: str
    tier: str
    confidence: float
    gate_action: str
    effective_tier: str
    latency_ms: float
    provider: str
    top_candidates: list[tuple[str, float]]


class IntentRouter:
    """Fast local Tier 1 intent classifier backed by quantized INT8 ONNX Runtime."""

    def __init__(
        self,
        model_dir: Path = TIER1_MODEL_DIR,
        intra_op_num_threads: int = 4,
        providers: list[str] | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        label_map_path = self.model_dir / "label_map.json"
        tokenizer_path = self.model_dir / "tokenizer.json"
        onnx_path = self.model_dir / "model_quantized.onnx"

        for required in (label_map_path, tokenizer_path, onnx_path):
            if not required.exists():
                raise FileNotFoundError(f"Missing Tier 1 router artifact: {required}")

        with label_map_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.id2label: dict[int, str] = {int(k): v for k, v in meta["id2label"].items()}
        self.intent_to_tier: dict[str, str] = dict(meta["intent_to_tier"])
        self.max_length: int = int(meta.get("max_length", 64))
        self.temperature: float = float(meta.get("temperature", 1.0))

        thresholds = meta.get("thresholds", {})
        self.high_threshold: float = float(thresholds.get("high", TIER1_CONFIDENCE_THRESHOLD))
        self.low_threshold: float = float(thresholds.get("low", TIER1_CONFIRM_THRESHOLD))

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=self.max_length)

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = intra_op_num_threads
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        requested_providers = providers or ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(
            str(onnx_path),
            sess_options=sess_opts,
            providers=requested_providers,
        )
        self.active_provider: str = self.session.get_providers()[0]

    @staticmethod
    def available_providers() -> list[str]:
        """Return ONNX Runtime execution providers installed in the environment."""
        return ort.get_available_providers()

    def _softmax(self, logits: np.ndarray) -> np.ndarray:
        scaled = logits / self.temperature
        scaled = scaled - np.max(scaled, axis=-1, keepdims=True)
        exp_x = np.exp(scaled)
        return exp_x / np.sum(exp_x, axis=-1, keepdims=True)

    def predict(self, text: str, top_k: int = 3) -> RouteResult:
        """Classify an utterance and return its calibrated 3-way routing decision."""
        cleaned = " ".join(text.strip().split())
        t0 = time.perf_counter()

        enc = self.tokenizer.encode(cleaned)
        input_ids = np.array([enc.ids], dtype=np.int64)
        attention_mask = np.array([enc.attention_mask], dtype=np.int64)

        logits = self.session.run(
            None,
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )[0][0]
        probs = self._softmax(logits)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        top_indices = np.argsort(probs)[::-1][:top_k]
        best_idx = int(top_indices[0])
        intent = self.id2label[best_idx]
        confidence = float(probs[best_idx])
        tier = self.intent_to_tier[intent]

        if confidence >= self.high_threshold:
            gate_action = "direct"
        elif confidence >= self.low_threshold:
            gate_action = "confirm"
        else:
            gate_action = "escalate"

        effective_tier = "tier2" if gate_action == "escalate" else tier
        candidates = [(self.id2label[int(i)], float(probs[int(i)])) for i in top_indices]

        return RouteResult(
            text=cleaned,
            intent=intent,
            tier=tier,
            confidence=confidence,
            gate_action=gate_action,
            effective_tier=effective_tier,
            latency_ms=latency_ms,
            provider=self.active_provider,
            top_candidates=candidates,
        )
