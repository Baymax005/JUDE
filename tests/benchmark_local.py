from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ROOT_DIR,
    assert_no_c_drive_leakage,
    configure_d_drive_env,
)
from app.router.intent_router import IntentRouter

BENCHMARK_PROMPT = (
    "In two concise sentences, explain why routing simple commands to a lightweight "
    "classifier before calling a language model improves desktop assistant latency."
)


def load_eval_samples() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Load 108 evenly spaced ID queries (12 per intent) and all 30 OOD queries."""
    id_csv = ROOT_DIR / "colab" / "intents_dataset.csv"
    ood_csv = ROOT_DIR / "colab" / "ood_test_utterances.csv"

    by_intent: dict[str, list[str]] = defaultdict(list)
    with id_csv.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_intent[row["intent"]].append(row["text"])

    id_samples: list[tuple[str, str]] = []
    for intent in sorted(by_intent):
        items = by_intent[intent]
        indices = np.linspace(0, len(items) - 1, 12, dtype=int)
        for idx in indices:
            id_samples.append((items[int(idx)], intent))

    ood_samples: list[tuple[str, str]] = []
    with ood_csv.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ood_samples.append((row["text"], row["category"]))

    return id_samples, ood_samples


def benchmark_tier1_cpu(
    id_samples: list[tuple[str, str]], ood_samples: list[tuple[str, str]]
) -> dict:
    """Benchmark Tier 1 INT8 ONNX across CPU thread counts + live smoke checks."""
    providers = IntentRouter.available_providers()
    print("=" * 68)
    print("  PART A: Tier 1 INT8 ONNX Local CPU Benchmark")
    print("=" * 68)
    print(f"Available ORT Providers : {providers}")
    print(
        "Execution Policy        : CPUExecutionProvider (lean runtime without multi-GB CUDA "
        "wheels; keeps 100% of GPU VRAM free for Tier 2 LLM)"
    )

    thread_results: list[dict] = []
    best_threads = 4
    best_mean = float("inf")
    best_router: IntentRouter | None = None

    for threads in (1, 2, 4, 6):
        router = IntentRouter(intra_op_num_threads=threads)
        for text, _ in id_samples[:10]:
            router.predict(text)

        latencies = [router.predict(text).latency_ms for text, _ in id_samples]
        mean_ms = float(np.mean(latencies))
        p50_ms = float(np.percentile(latencies, 50))
        p95_ms = float(np.percentile(latencies, 95))
        entry = {
            "threads": threads,
            "mean_ms": round(mean_ms, 2),
            "p50_ms": round(p50_ms, 2),
            "p95_ms": round(p95_ms, 2),
        }
        thread_results.append(entry)
        print(
            f"  threads={threads} -> Mean: {mean_ms:6.2f} ms | "
            f"P50: {p50_ms:6.2f} ms | P95: {p95_ms:6.2f} ms"
        )
        if mean_ms < best_mean:
            best_mean = mean_ms
            best_threads = threads
            best_router = router

    assert best_router is not None
    id_results = [best_router.predict(text) for text, _ in id_samples]
    id_correct = sum(1 for res, (_, truth) in zip(id_results, id_samples) if res.intent == truth)
    id_acc = id_correct / len(id_samples)

    ood_results = [best_router.predict(text) for text, _ in ood_samples]
    ood_direct = sum(1 for r in ood_results if r.gate_action == "direct")
    ood_confirm = sum(1 for r in ood_results if r.gate_action == "confirm")
    ood_escalate = sum(1 for r in ood_results if r.gate_action == "escalate")

    print(f"\nFastest CPU Setting     : intra_op_num_threads={best_threads} ({best_mean:.2f} ms mean)")
    print(f"Sample Accuracy (n=108) : {id_correct}/{len(id_samples)} ({id_acc * 100:.2f}%)")
    print(
        f"OOD Gate Breakdown (n=30): direct={ood_direct}, confirm={ood_confirm}, "
        f"escalate={ood_escalate} | blocked<0.80={(ood_confirm + ood_escalate) / len(ood_samples) * 100:.1f}%"
    )

    smoke_queries = [
        ("Save my project presentation room as Hall B", "SAVE_MEMORY"),
        ("What room did I save for my presentation?", "RECALL_MEMORY"),
        ("Launch Windows Calculator", "OPEN_TARGET"),
        ("Zip up my semester notes folder", "COMPRESS_FILES"),
        ("How much free space is left on my drive?", "SYSTEM_INFO"),
        ("Rewrite the selected paragraph in Word to sound formal", "EDIT_DOCUMENT"),
        ("Explain this Python traceback on my screen", "EXPLAIN_SCREEN"),
        ("Search the web for Python 3.13 release notes", "WEB_SEARCH"),
        ("Explain precision versus recall in one sentence", "GENERAL_CHAT"),
        ("uhh wait never mind", "OOD"),
        ("order a large pepperoni pizza", "OOD"),
        ("fix it", "OOD"),
    ]
    print("\n--- Live Smoke Test ---")
    for query, expected in smoke_queries:
        res = best_router.predict(query)
        print(
            f"  [{expected:<14}] pred={res.intent:<14} conf={res.confidence:.3f} "
            f"gate={res.gate_action:<8} eff={res.effective_tier:<5} "
            f"({res.latency_ms:.2f} ms) | {query!r}"
        )

    best_entry = next(e for e in thread_results if e["threads"] == best_threads)
    return {
        "available_providers": providers,
        "active_provider": best_router.active_provider,
        "thread_sweep": thread_results,
        "best_threads": best_threads,
        "best_latency_ms": {
            "mean": best_entry["mean_ms"],
            "p50": best_entry["p50_ms"],
            "p95": best_entry["p95_ms"],
        },
        "sample_accuracy_108": round(id_acc, 4),
        "ood_gate_30": {
            "direct": ood_direct,
            "confirm": ood_confirm,
            "escalate": ood_escalate,
            "blocked_below_0_80": round((ood_confirm + ood_escalate) / len(ood_samples), 4),
        },
    }


def _ollama_chat(num_gpu: int) -> dict:
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "keep_alive": "2m",
            "options": {
                "num_gpu": num_gpu,
                "temperature": 0.0,
                "num_predict": 64,
            },
            "messages": [{"role": "user", "content": BENCHMARK_PROMPT}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _read_ollama_ps() -> tuple[float, float]:
    ps_resp = requests.get(f"{OLLAMA_BASE_URL}/api/ps", timeout=15)
    ps_resp.raise_for_status()
    models = ps_resp.json().get("models", [])
    for m in models:
        name = m.get("name", "")
        if name == OLLAMA_MODEL or name.startswith(f"{OLLAMA_MODEL}:"):
            size_mb = m.get("size", 0) / (1024 * 1024)
            vram_mb = m.get("size_vram", 0) / (1024 * 1024)
            return round(size_mb, 1), round(vram_mb, 1)
    return 0.0, 0.0


def _run_ollama_mode(label: str, num_gpu: int) -> dict:
    # Warmup call loads weights into GPU or CPU and populates /api/ps
    _ = _ollama_chat(num_gpu=num_gpu)
    size_mb, vram_mb = _read_ollama_ps()

    tok_rates: list[float] = []
    gen_ms_list: list[float] = []
    total_ms_list: list[float] = []
    sample_reply = ""

    for _ in range(3):
        data = _ollama_chat(num_gpu=num_gpu)
        eval_count = int(data.get("eval_count", 0))
        eval_ns = max(int(data.get("eval_duration", 1)), 1)
        total_ns = max(int(data.get("total_duration", 1)), 1)
        tok_rates.append(eval_count / (eval_ns / 1e9))
        gen_ms_list.append(eval_ns / 1e6)
        total_ms_list.append(total_ns / 1e6)
        sample_reply = data.get("message", {}).get("content", "").strip()

    result = {
        "mode": label,
        "num_gpu": num_gpu,
        "model_size_mb": size_mb,
        "vram_used_mb": vram_mb,
        "tokens_per_sec": round(float(np.mean(tok_rates)), 2),
        "gen_ms": round(float(np.mean(gen_ms_list)), 1),
        "total_ms": round(float(np.mean(total_ms_list)), 1),
        "sample_reply": sample_reply,
    }
    print(
        f"  {label:<18} | VRAM={vram_mb:6.1f}/{size_mb:6.1f} MB | "
        f"Speed={result['tokens_per_sec']:6.2f} tok/s | "
        f"Gen={result['gen_ms']:7.1f} ms | Total={result['total_ms']:7.1f} ms"
    )
    return result


def benchmark_tier2_ollama() -> dict:
    """Benchmark local Ollama 'jude' 1.5B on GPU vs. CPU."""
    print("\n" + "=" * 68)
    print(f"  PART B: Tier 2 Local Ollama ({OLLAMA_MODEL}) GPU vs. CPU Benchmark")
    print("=" * 68)

    tags_resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
    tags_resp.raise_for_status()
    models = [m.get("name", "") for m in tags_resp.json().get("models", [])]
    if not any(m == OLLAMA_MODEL or m.startswith(f"{OLLAMA_MODEL}:") for m in models):
        raise RuntimeError(f"Model {OLLAMA_MODEL!r} not found in Ollama tags: {models}")

    gpu_stats = _run_ollama_mode("GPU (num_gpu=99)", num_gpu=99)
    cpu_stats = _run_ollama_mode("CPU (num_gpu=0)", num_gpu=0)

    speedup = round(gpu_stats["tokens_per_sec"] / max(cpu_stats["tokens_per_sec"], 0.01), 2)
    print(f"\nGPU vs. CPU Generation Speedup: {speedup:.2f}x")
    print(f"Sample Reply: {gpu_stats['sample_reply']}")

    return {
        "model": OLLAMA_MODEL,
        "gpu": gpu_stats,
        "cpu": cpu_stats,
        "gpu_speedup_x": speedup,
    }


def main() -> int:
    configure_d_drive_env()
    assert_no_c_drive_leakage()

    id_samples, ood_samples = load_eval_samples()
    tier1_stats = benchmark_tier1_cpu(id_samples, ood_samples)
    tier2_stats = benchmark_tier2_ollama()

    out_path = ROOT_DIR / "reports" / "local_benchmark.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"tier1_cpu": tier1_stats, "tier2_ollama": tier2_stats}, f, indent=2)

    print(f"\n[OK] Saved local benchmark report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
