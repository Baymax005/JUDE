# J.U.D.E. — Local-First Two-Tier Desktop AI Assistant

[![Tier 1 Colab](https://img.shields.io/badge/Tier%201%20DistilBERT-Open%20in%20Colab-F9AB00?logo=googlecolab&logoColor=white)](https://colab.research.google.com/github/Baymax005/JUDE/blob/main/colab/train_tier1_router.ipynb)
[![Tier 2 QLoRA Colab](https://img.shields.io/badge/Tier%202%20J.U.D.E.--3B%20QLoRA-Open%20in%20Colab-0EA5E9?logo=googlecolab&logoColor=white)](https://colab.research.google.com/github/Baymax005/JUDE/blob/main/colab/train_tier2_jude_lora.ipynb)

**J.U.D.E.** is a local-first, voice-and-screen-aware Windows 11 desktop AI agent (with a **VMware Linux guest SSH/OCR bridge**) built on a **two-tier cognitive cascade**, combining a sub-10 ms DistilBERT router with a custom 4-bit **QLoRA fine-tuned `J.U.D.E.-3B` tool-calling agent**.

---

## Two-Tier Architecture

| Tier | Engine | Role | Latency Target |
| :--- | :--- | :--- | :--- |
| **Tier 1 (System 1 — Fast Reflex)** | Fine-tuned `distilbert-base-uncased` (INT8 ONNX via `onnxruntime`) + deterministic Python handlers | Classifies intent locally and executes high-confidence (`>= 0.88`) fast commands directly (`SAVE_MEMORY`, `RECALL_MEMORY`, `OPEN_TARGET`, `COMPRESS_FILES`, `SYSTEM_INFO`, instant window/terminal control) | ~6–10 ms on CPU |
| **Tier 2 (System 2 — Tool-Calling Agent)** | Custom QLoRA fine-tuned **`J.U.D.E.-3B`** (`Qwen2.5-3B-Instruct` base, `r=16`, `Q4_K_M` GGUF, `num_ctx=4096`) in local Ollama (`keep_alive="3m"`) | Handles all 18 Windows 11 + VMware Linux tools (Word COM read/create/edit, Chrome profiles, terminal, Explorer, UI inspection with password masking, VMware WinRT OCR & SSH), habit learning, and honest out-of-scope refusals | ~1–4 s local |

### Confidence Gate & Silent Escalation
Tier 1 calibrated softmax probabilities drive a non-intrusive routing gate configured in [`app/config.py`](app/config.py):

- **`confidence >= 0.88`** → **Direct Route**: Execute Tier 1 deterministic Python handler immediately (with explicit confirmation modals retained for file compression, deletion/closes, Word writes, and `sudo`/modifying Linux VM commands).
- **`confidence < 0.88`** → **Silent Tier 2 Escalation**: Hand off smoothly to the **Tier 2 `J.U.D.E.-3B` Tool Agent** with live active-window and learned preference context (eliminating interrupting confirmation popups on conversational queries).

---

## Intent Taxonomy & Datasets

### 1. In-Distribution Intent Dataset ([`colab/intents_dataset.csv`](colab/intents_dataset.csv))
**1,080 synthetic utterances** (`9` balanced intents × `120` utterances each), mixing clean `typed` queries (~70/class) and `voice` ASR-style transcripts (~50/class), plus semantic paraphrases and cross-class hard negatives:

| Intent | Tier | Scope |
| :--- | :--- | :--- |
| `SAVE_MEMORY` | `tier1` | Store synthetic notes, reminders, schedules, and preferences in local SQLite |
| `RECALL_MEMORY` | `tier1` | Retrieve previously saved notes, contacts, or reminders |
| `OPEN_TARGET` | `tier1` | Open local folders, files, or desktop applications |
| `COMPRESS_FILES` | `tier1` | Zip, archive, or bundle files and folders into `.zip` archives |
| `SYSTEM_INFO` | `tier1` | Query local drive space, RAM/CPU usage, battery level, time/date, or network status |
| `EDIT_DOCUMENT` | `tier2` | Rewrite, shorten, polish, fix grammar, or insert content in an open Word document / selection |
| `EXPLAIN_SCREEN` | `tier2` | Read and explain visible errors, stack traces, dialogs, or code currently on screen |
| `WEB_SEARCH` | `tier2` | Search the web (`ddgs` + `trafilatura`) and summarize retrieved pages |
| `GENERAL_CHAT` | `tier2` | General Q&A, conceptual explanations, standalone drafting, and conversation |

> **Privacy Note:** All dataset utterances in `colab/` are synthetic placeholders. Personal user memory (`data/memory.db`), caches, models, and logs are gitignored and remain strictly local.

### 2. Out-of-Distribution & Ambiguity Test Set ([`colab/ood_test_utterances.csv`](colab/ood_test_utterances.csv))
**30 unseen OOD utterances** (`10` `asr_noise`, `10` `unsupported_action`, `10` `ambiguous`) used strictly during evaluation to validate the 3-way confidence gate and temperature scaling calibration.

---

## Training Tier 1 on Google Colab

1. Click the **Open in Colab** badge above (or open [`train_tier1_router.ipynb`](https://colab.research.google.com/github/Baymax005/JUDE/blob/main/colab/train_tier1_router.ipynb)).
2. Select **Runtime → Change runtime type → T4 GPU**.
3. Click **Runtime → Run all**.

### Evaluation & Report Outputs Produced
The notebook runs on a reproducible stratified **80% / 10% / 10%** split (`SEED = 42`) and outputs:
- **Baseline vs. DistilBERT Comparison (`baseline_vs_distilbert.csv`):** Tuned **TF-IDF + Logistic Regression** baseline vs. fine-tuned **`distilbert-base-uncased`** on the exact same split.
- **Full Per-Class Metrics:** Precision, Recall, and F1-Score (`classification_report`) plus `confusion_matrix.png`.
- **Training Dynamics:** `loss_curve.png` (Training vs. Validation Loss + Validation Accuracy & Macro-F1 per epoch).
- **Calibration & OOD Validation:** Temperature scaling (`T` fitted on validation NLL), ECE before/after scaling, 3-way gate breakdown on ID vs. OOD queries (`ood_predictions.csv`), and `confidence_histogram_ood.png`.
- **FP32 PyTorch vs. INT8 ONNX Benchmark (`fp32_vs_int8_benchmark.png`):** Model size (MB), single-query CPU latency (mean/p50/p95 ms), and test accuracy/Macro-F1.
- **Downloadable Archives:**
  - `tier1_router_bundle.zip` (`model_quantized.onnx`, `tokenizer.json`, `tokenizer_config.json`, `label_map.json`, `metrics.json`)
  - `cl461_report_assets.zip` (all PNG charts, CSV tables, and `metrics.json`)

After downloading `tier1_router_bundle.zip`, extract its contents into `models/tier1_router/`.

---

## Evaluation & Benchmark Results

All classification metrics below were measured on the held-out stratified **10% test split (`n = 108`, `SEED = 42`)** and the unseen **OOD / ambiguity test set (`n = 30`)**. Local hardware measurements were captured on a **Local 6-Core x86-64 CPU** and **Local 4 GB VRAM CUDA GPU**.

### 1. Intent Classification (`n = 108` Held-Out Test Split)

| Model | Test Accuracy | Macro Precision | Macro Recall | Macro-F1 | Typed Acc | Voice Acc |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **TF-IDF + Logistic Regression (`C=5.0`)** | `99.07%` | `0.9915` | `0.9907` | `0.9907` | `98.44%` | `100.00%` |
| **Fine-Tuned DistilBERT (FP32, best epoch 3)** | `98.15%` | `0.9829` | `0.9815` | `0.9814` | `98.44%` | `97.73%` |
| **INT8 ONNX DistilBERT (CPU runtime)** | `96.30%` | — | — | `0.9632` | — | — |

> While a tuned linear baseline matches DistilBERT within a single sample on clean in-vocabulary test queries, DistilBERT provides contextual subword generalization on unseen paraphrases and clean probability separation for OOD gating.

### 2. Calibration & 3-Way Confidence Gate (`n = 108` ID + `n = 30` OOD)

| Metric | Measured Value |
| :--- | :---: |
| **ID Direct-Route Accuracy (`confidence >= 0.80`)** | **`100.00%`** |
| **OOD Blocked from Direct Execution (`confidence < 0.80`)** | **`96.7%`** (`29 / 30`) |
| **OOD Auto-Escalated to Tier 2 (`confidence < 0.55`)** | **`53.3%`** (`16 / 30` escalated, `13 / 30` confirm) |
| **ID vs. OOD ROC-AUC (Max Softmax)** | **`0.9469`** |
| **Validation ECE (`T = 1.0`)** | `0.1045` |

### 3. Tier 1 Footprint & CPU Inference Latency

| Runtime | Footprint | Mean Latency | P50 Latency | P95 Latency |
| :--- | :---: | :---: | :---: | :---: |
| **PyTorch FP32 (Colab 2-vCPU)** | `255.45 MB` | `114.56 ms` | `91.72 ms` | `229.71 ms` |
| **ONNX INT8 (Colab 2-vCPU)** | `132.47 MB` (`-48.14%`) | `51.49 ms` | `44.05 ms` | `100.72 ms` |
| **ONNX INT8 (Local 6-Core x86-64 CPU, `threads=4`)** | `132.47 MB` | **`6.61 ms`** | **`6.58 ms`** | **`8.81 ms`** |

*Running Tier 1 on `CPUExecutionProvider` keeps the runtime free of multi-GB CUDA dependencies and leaves 100% of GPU VRAM available for Tier 2.*

### 4. Tier 2 Local `jude` (1.5B) GPU vs. CPU Inference

| Execution Mode | VRAM Used | Generation Speed | Generation Latency | Total Latency |
| :--- | :---: | :---: | :---: | :---: |
| **Local 6-Core x86-64 CPU (`num_gpu=0`)** | `0.0 / 1118.8 MB` | `19.23 tok/s` | `1153.4 ms` | `1239.6 ms` |
| **Local 4 GB VRAM CUDA GPU (`num_gpu=99`)** | `1115.2 / 1115.2 MB` | **`24.11 tok/s`** (`1.25x`) | **`912.4 ms`** | **`970.0 ms`** |

*Tier 1 (`6.61 ms` mean) resolves deterministic commands ~147× faster than waking Tier 2 (`970.0 ms` total on GPU).*

### 5. Tier 2 QLoRA Fine-Tuning Benchmark (`n = 90` Held-Out Tool-Calling Eval)

`Qwen2.5-3B-Instruct` was fine-tuned with **4-bit QLoRA** (`r = 16`, `lora_alpha = 32`, `MAX_SEQ_LENGTH = 2048`, `2` epochs, `29.9M` trainable parameters) in [`colab/train_tier2_jude_lora.ipynb`](colab/train_tier2_jude_lora.ipynb) on a blended corpus of ungated [`NousResearch/hermes-function-calling-v1`](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1) and J.U.D.E.'s synthetic OS/VMware/honest-refusal split ([`colab/build_tier2_dataset.py`](colab/build_tier2_dataset.py)), then exported to **`Q4_K_M` GGUF (`1.93 GB`)** for local Ollama.

| Evaluation Metric (`n = 90`) | Base `Qwen2.5-3B` | QLoRA Fine-Tuned `J.U.D.E.-3B` | Delta |
| :--- | :---: | :---: | :---: |
| **JSON / Format Validity** | `95.56%` | **`100.00%`** | `+4.44%` |
| **Tool Selection Accuracy** | `76.67%` | **`100.00%`** | `+23.33%` |
| **Argument Match Accuracy** | `55.56%` | **`98.89%`** | `+43.33%` |
| **Honest Refusal Rate (Out-of-Scope)** | `61.11%` | **`100.00%`** | `+38.89%` |

### Training & Evaluation Charts

| Tier 1 Training vs. Validation Loss | Tier 1 Test Set Confusion Matrix |
| :---: | :---: |
| ![Training vs Validation Loss](docs/benchmarks/loss_curve.png) | ![Confusion Matrix](docs/benchmarks/confusion_matrix.png) |

| Tier 1 Confidence Calibration & OOD Gate | Tier 1 FP32 PyTorch vs. INT8 ONNX |
| :---: | :---: |
| ![Confidence Histogram OOD](docs/benchmarks/confidence_histogram_ood.png) | ![FP32 vs INT8 Benchmark](docs/benchmarks/fp32_vs_int8_benchmark.png) |

| Tier 2 QLoRA Loss Curve | Tier 2 Base `Qwen2.5-3B` vs. `J.U.D.E.-3B` |
| :---: | :---: |
| ![Tier 2 QLoRA Loss Curve](docs/benchmarks/tier2_loss_curve.png) | ![Tier 2 Before vs After Benchmark](docs/benchmarks/tier2_before_vs_after.png) |

---

## Portable Local Setup & Verification

`run_jude.ps1`, `launch_jude.vbs` (silent zero-terminal launch), and `launch_jude.bat` keep virtual environments, caches, temporary files, SQLite memory, backups, and Ollama model paths self-contained inside the project folder:

```powershell
# Launch the floating J.U.D.E. desktop widget (or double-click launch_jude.vbs / launch_jude.bat)
.\run_jude.ps1

# Verify environment isolation + Tier 1 ONNX router
.\run_jude.ps1 -m app.main --check

# Run a single command in CLI mode
.\run_jude.ps1 -m app.main --once "What time is it?"

# Verify full environment, imports, and local Ollama 'jude' model
.\run_jude.ps1 .\verify_env.py

# Run Phase 2 automated test suite
.\run_jude.ps1 .\tests\test_phase2.py
```

---

## Floating J.U.D.E. Desktop Widget & Safety Controls

- **Pure PySide6 `QPainter` Avatar (`0 MB` external images):** Renders a HiDPI glossy porcelain-white capsule head (`●—●`) with periodic eye blinking, smooth floating bob, and color-coded status ring (`IDLE` cyan, `Listening` teal, `Tier 1` emerald, `Tier 2` violet rotating arc, `Confirm` amber, `Speaking` sky-blue, `Error` red).
- **Hands-Free Voice & Offline Neural TTS:** Local `faster-whisper` (`small.en` INT8) listens on-device with echo suppression while J.U.D.E. speaks via **100% offline Piper ONNX neural TTS** (with Win32 SAPI5 fallback). Click J.U.D.E. to expand/collapse the 2-row chat HUD, drag to reposition anywhere on screen, or manage memories & learned habits in the 2-tab **Memory Manager**.
- **Safety-by-Design Confirmation Gate:** File compression (`COMPRESS_FILES`), Word document creation/edits (with editable preview + `.undo.txt` snapshots saved to `backups/`), closing windows (`CLOSE_APP` allowing File Explorer folder windows only), and `sudo`/modifying VMware Linux SSH commands pause for explicit confirmation before executing.

---

## Phase 3 CI/CD Packaging Note (`.exe` Builds)

When building Windows desktop releases via GitHub Actions (`.github/workflows/build-exe.yml`), J.U.D.E. will be packaged using **PyInstaller `--onedir`** (distributed as a release `.zip`) rather than `--onefile`. Using `--onedir` keeps runtime artifacts self-contained and avoids unpacking `_MEI*` temporary folders at runtime.
