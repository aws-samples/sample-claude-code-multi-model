# Serving on a p5en.48xlarge (8x H200) - extra CUDA fixes

> Companion note to [SKILL.md](SKILL.md). The vLLM scripts and the model guides under [`self-hosted/vllm/`](../../../self-hosted/vllm/) were verified on the **reference node**: g6e.12xlarge (4x L40S). On a **p5en.48xlarge (8x H200 141GB, NVSwitch)** several additional issues surface that the reference node never hits, because 8-GPU tensor parallelism over NVSwitch activates code paths (DeepGemm JIT, FlashInfer mnnvl allreduce) that JIT-compile CUDA at server startup. This file records every extra change needed there. If you are on the reference 4x L40S node you do **not** need any of this; if you are on a p5en (or any 8x H200 / NVSwitch box), you do.

All paths below assume the venv and caches live on the large ephemeral NVMe (`/opt/dlami/nvme`) rather than the small root disk - see "Disk layout" first.

## 0. Driver alignment (prerequisite, one-time)

The DLAMI shipped a **mismatched NVIDIA driver**: the loaded kernel module was `595.71.05` while the userspace libraries had been upgraded (likely by unattended-upgrades) to `610.43.02`, so `nvidia-smi` failed with `Failed to initialize NVML: Driver/library version mismatch`. `nvidia-fabricmanager` (mandatory on an NVSwitch box) was apt-held at 595.

Fix - align the whole stack to 610.43.02 and reboot:

```bash
sudo apt-get install -y --allow-change-held-packages \
  nvidia-dkms-610=610.43.02-0ubuntu0.24.04.1 \
  nvidia-fabricmanager=610.43.02-1ubuntu1 \
  nvidia-utils-610=610.43.02-0ubuntu0.24.04.1
sudo apt-mark hold nvidia-fabricmanager   # re-pin FM (preserve the original hold intent)
sudo reboot                                # required: unload 595, load the freshly built 610 module
```

After reboot, verify before doing anything else:

```bash
nvidia-smi --query-gpu=index,name,driver_version --format=csv   # expect 8x H200 @ 610.43.02
systemctl is-active nvidia-fabricmanager                        # expect: active
nvidia-smi -q | grep -A2 Fabric                                 # expect: State: Completed / Status: Success
```

The NVLink fabric State must be `Completed` - tensor parallelism across 8 GPUs depends on it.

## Disk layout: put the venv, caches, and weights on the NVMe

On this node the root disk `/` is tiny (~29 GB). The vLLM venv (torch + CUDA wheels, several GB) and especially the model weights (Kimi-K2.7-Code is ~555 GB on disk) must not go there. Use the 27 TB ephemeral NVMe at `/opt/dlami/nvme`:

```bash
mkdir -p /opt/dlami/nvme/{vllm-env,uv-cache,hf-cache,tmp,cuda-link}
export VLLM_ENV=/opt/dlami/nvme/vllm-env      # vllm-install.sh + vllm-serve.sh both honor this
export UV_CACHE_DIR=/opt/dlami/nvme/uv-cache
export TMPDIR=/opt/dlami/nvme/tmp
# HF_HOME auto-defaults to /opt/dlami/nvme/hf-cache in vllm-serve.sh when the volume exists.
```

`/opt/dlami/nvme` is **ephemeral** - wiped on instance stop/terminate. Weights re-download after a stop; that is the right trade for a serving box.

## The CUDA startup fixes (the core of this note)

On 8x H200 + NVSwitch, vLLM JIT-compiles CUDA at startup in two places that the reference node never triggers. Both fail out of the box; both are fixed purely with environment variables passed to `vllm-serve.sh`.

### Fix 1 - `ninja` must be on PATH (DeepGemm + FlashInfer JIT)

DeepGemm and FlashInfer shell out to the **`ninja` executable** by name to build CUDA kernels. `vllm-install.sh` pip-installs the `ninja` package into the venv (`$VLLM_ENV/bin/ninja`), but `vllm-serve.sh` invokes vLLM by absolute path without activating the venv, so `bin/` is not on the subprocess PATH. Symptom:

```
RuntimeError: Worker failed with error '[Errno 2] No such file or directory: 'ninja''
```

(occurs at KV-cache init, after weight loading). Fix - put the venv bin (and the CUDA toolkit bin, for `nvcc`) on PATH:

```bash
export CUDA_HOME=/opt/pytorch/cuda                 # the DLAMI's real toolkit (has nvcc); /usr/local/cuda does NOT exist
export PATH="$VLLM_ENV/bin:$CUDA_HOME/bin:$PATH"
```

### Fix 2 - CUDA libs must be linkable (`-lcudart` / `-lcuda`)

Once `ninja` runs, FlashInfer's **mnnvl allreduce** kernel (`trtllm_mnnvl_comm`, auto-selected on an NVSwitch box) JIT-compiles and fails at the **link** step:

```
/usr/bin/ld: cannot find -lcudart: No such file or directory
/usr/bin/ld: cannot find -lcuda:   No such file or directory
collect2: error: ld returned 1 exit status
RuntimeError: Ninja build failed. ... Engine core initialization failed.
```

(occurs late, after CUDA graph capture). The linker wants **unversioned** `libcudart.so` / `libcuda.so`, but this node only has versioned files:
- `libcudart.so.13` inside the venv at `nvidia/cu13/lib/`
- `libcuda.so.1 -> libcuda.so.610.43.02` in `/usr/lib/x86_64-linux-gnu/` (driver)

Fix - create a link dir of unversioned symlinks and expose it via `LIBRARY_PATH` (link time) and `LD_LIBRARY_PATH` (runtime):

```bash
LINKDIR=/opt/dlami/nvme/cuda-link
VENV_CU13="$VLLM_ENV/lib/python3.12/site-packages/nvidia/cu13/lib"
mkdir -p "$LINKDIR"
ln -sf "$VENV_CU13/libcudart.so.13"            "$LINKDIR/libcudart.so"
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1  "$LINKDIR/libcuda.so"

export LIBRARY_PATH="$LINKDIR:$VENV_CU13:/usr/lib/x86_64-linux-gnu:${LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="$LINKDIR:$VENV_CU13:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
```

Verify the link resolves before launching (fast sanity check):

```bash
echo 'int main(){return 0;}' > /tmp/t.c
gcc /tmp/t.c -L"$LINKDIR" -lcudart -lcuda -o /tmp/t.out && echo "LINK OK"
```

> Alternative: set `VLLM_FLASHINFER_ALLREDUCE_BACKEND` away from `mnnvl`, or otherwise disable the FlashInfer fused allreduce, so it never JIT-compiles. vLLM falls back to the CUSTOM/PYNCCL allreduce backends (already available). Fix 2 above keeps the optimized allreduce path instead of dropping it; prefer it unless you want to skip the JIT entirely.

### Fix 3 - FP8 models (GLM-5.2) also need `libnvrtc.so`, in `$CUDA_HOME/lib64`

Kimi-K2.7-Code needs only Fix 1 + Fix 2. **GLM-5.2-FP8 needs one more.** Its FP8 path JIT-compiles an extra FlashInfer kernel, `fp8_blockscale_gemm_90` (a CUTLASS/DeepGemm blockscale GEMM), that links against **NVRTC** (NVIDIA Runtime Compilation) in addition to cudart/cuda. Symptom (late, after CUDA graph capture, same shape as Fix 2 but a different lib):

```
/usr/bin/ld: cannot find -lnvrtc: No such file or directory
collect2: error: ld returned 1 exit status
RuntimeError: Ninja build failed ... fp8_blockscale_gemm_90.so ... Engine core initialization failed.
```

Two things make this distinct from Fix 2:

1. **A different missing lib - `libnvrtc`.** Only `libnvrtc.so.13` exists (in the venv's `nvidia/cu13/lib`); the linker wants unversioned `libnvrtc.so`.
2. **FlashInfer's link command hardcodes its `-L` search dirs to `$CUDA_HOME/lib64` and `$CUDA_HOME/lib64/stubs`** (visible in the failing `c++ ... -shared -L/opt/pytorch/cuda/lib64 -L/opt/pytorch/cuda/lib64/stubs -lcudart -lcuda -lnvrtc ...` line). On the DLAMI, `/opt/pytorch/cuda` has a `lib` dir but **no `lib64`** - so even `-lcudart`/`-lcuda` are only found via `LIBRARY_PATH` from Fix 2, and `-lnvrtc` is not found at all. The robust fix is to create `$CUDA_HOME/lib64` (+ `stubs`) and populate it with the unversioned symlinks the build command expects.

Fix - add `libnvrtc.so` to the Fix-2 link dir AND materialize the symlinks in `$CUDA_HOME/lib64` where FlashInfer actually looks:

```bash
LINKDIR=/opt/dlami/nvme/cuda-link
VENV_CU13="$VLLM_ENV/lib/python3.12/site-packages/nvidia/cu13/lib"

# (a) extend the Fix-2 link dir with nvrtc
ln -sf "$VENV_CU13/libnvrtc.so.13" "$LINKDIR/libnvrtc.so"

# (b) create + populate $CUDA_HOME/lib64 and its stubs (the dirs FlashInfer's -L points at).
#     /opt/pytorch/cuda has only lib/, no lib64 - so we make it. $CUDA_HOME/lib is writable
#     without sudo on the DLAMI; if lib64 is not, prefix these with sudo.
mkdir -p "$CUDA_HOME/lib64/stubs"
ln -sf "$VENV_CU13/libcudart.so.13"            "$CUDA_HOME/lib64/libcudart.so"
ln -sf "$VENV_CU13/libnvrtc.so.13"             "$CUDA_HOME/lib64/libnvrtc.so"
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1  "$CUDA_HOME/lib64/libcuda.so"
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1  "$CUDA_HOME/lib64/stubs/libcuda.so"

# then add lib64 + stubs to LIBRARY_PATH (belt and suspenders):
export LIBRARY_PATH="$LINKDIR:$CUDA_HOME/lib64:$CUDA_HOME/lib64/stubs:$VENV_CU13:/usr/lib/x86_64-linux-gnu:${LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="$LINKDIR:$CUDA_HOME/lib64:$VENV_CU13:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
```

Verify all three resolve before launching:

```bash
echo 'int main(){return 0;}' > /tmp/t.c
gcc /tmp/t.c -L"$LINKDIR" -lcudart -lcuda -lnvrtc -o /tmp/t.out && echo "LINK OK"
```

If a prior boot already failed on this, clear the stale JIT cache so it rebuilds cleanly: `rm -rf ~/.cache/flashinfer/*/*/cached_ops/fp8_blockscale_gemm_90`.

GLM-5.2 verified serving with this: `zai-org/GLM-5.2-FP8`, TP=8, `MAX_MODEL_LEN=300000` (passes the >=200K gate), `GPU_MEM_UTIL=0.95`, `glm47` tool+reasoning parsers. Runtime: GPU KV cache ~413,000 tokens, max concurrency 1.38x at 300K.

Note the pre-existing DLAMI fixes the serve script already applies automatically and that you should NOT undo: `VLLM_USE_FLASHINFER_SAMPLER=0` (native sampler, no runtime nvcc) and the `CUDA_HOME` fallback to `/opt/pytorch/cuda`.

## Putting it together - the full p5en launch

`vllm-serve.sh` inherits the caller's environment, so export everything above first, then run it with the model guide's parameters ([kimi-k2.7-code.md](../../../self-hosted/vllm/models/kimi-k2.7-code.md), TP=8):

```bash
cd self-hosted/vllm/scripts

export VLLM_ENV=/opt/dlami/nvme/vllm-env
export CUDA_HOME=/opt/pytorch/cuda
export TMPDIR=/opt/dlami/nvme/tmp
export PATH="$VLLM_ENV/bin:$CUDA_HOME/bin:$PATH"                        # Fix 1

LINKDIR=/opt/dlami/nvme/cuda-link                                       # Fix 2 + Fix 3
VENV_CU13="$VLLM_ENV/lib/python3.12/site-packages/nvidia/cu13/lib"
mkdir -p "$LINKDIR"
ln -sf "$VENV_CU13/libcudart.so.13"           "$LINKDIR/libcudart.so"
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1 "$LINKDIR/libcuda.so"
ln -sf "$VENV_CU13/libnvrtc.so.13"            "$LINKDIR/libnvrtc.so"    # Fix 3 (FP8 models)

# Fix 3: FlashInfer's FP8 kernel link cmd hardcodes -L$CUDA_HOME/lib64[/stubs], which
# does not exist on the DLAMI (only $CUDA_HOME/lib). Create + populate it.
mkdir -p "$CUDA_HOME/lib64/stubs"
ln -sf "$VENV_CU13/libcudart.so.13"           "$CUDA_HOME/lib64/libcudart.so"
ln -sf "$VENV_CU13/libnvrtc.so.13"            "$CUDA_HOME/lib64/libnvrtc.so"
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1 "$CUDA_HOME/lib64/libcuda.so"
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1 "$CUDA_HOME/lib64/stubs/libcuda.so"

export LIBRARY_PATH="$LINKDIR:$CUDA_HOME/lib64:$CUDA_HOME/lib64/stubs:$VENV_CU13:/usr/lib/x86_64-linux-gnu:${LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="$LINKDIR:$CUDA_HOME/lib64:$VENV_CU13:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"

# --- Kimi-K2.7-Code (needs Fix 1 + Fix 2; Fix 3 is harmless to leave in) ---
MODEL="moonshotai/Kimi-K2.7-Code" \
SERVED_NAME="kimi-k2.7-code" \
TP=8 PORT=8000 MAX_MODEL_LEN=131072 GPU_MEM_UTIL=0.90 \
TOOL_PARSER="kimi_k2" REASONING_PARSER="kimi_k2" \
EXTRA_ARGS="--trust-remote-code" \
  ./vllm-serve.sh

# --- OR GLM-5.2-FP8 (needs Fix 1 + Fix 2 + Fix 3; passes the >=200K gate at 300K) ---
MODEL="zai-org/GLM-5.2-FP8" \
SERVED_NAME="glm-5.2" \
TP=8 PORT=8000 MAX_MODEL_LEN=300000 GPU_MEM_UTIL=0.95 \
TOOL_PARSER="glm47" REASONING_PARSER="glm47" \
EXTRA_ARGS="--trust-remote-code" \
  ./vllm-serve.sh
```

## Startup timing and what "healthy" looks like

First boot: ~10-15 min weight download (~555 GB at ~1 GB/s with an HF token) + ~4 min weight load (64 shards) + torch.compile + CUDA graph capture (51 graphs). Subsequent boots skip the download. Healthy-boot signposts in `self-hosted/vllm/logs/vllm-serve.log`:

- `Using CompressedTensorsWNA16MarlinMoEMethod` / `MARLIN WNA16 MoE backend` (correct quantization)
- `Using FLASH_ATTN_MLA attention backend` (correct for the DeepSeek-V3/MLA arch)
- `Loading safetensors checkpoint shards: 100%`
- `Capturing CUDA graphs ... 51/51`
- `GPU KV cache size: ~740,000 tokens` / `Maximum concurrency for 131072 tokens per request: ~5.6x`
- `Server ready at http://127.0.0.1:8000/v1`

## Quick failure -> fix reference

| Symptom in the log | Cause | Fix |
|---|---|---|
| `NVML: Driver/library version mismatch` | kernel module vs userspace driver skew | Section 0 (align to 610 + reboot) |
| `No space left` during download, or root disk fills | weights/venv on the small root disk | Disk layout (use `/opt/dlami/nvme`) |
| `No such file or directory: 'ninja'` | ninja binary not on subprocess PATH | Fix 1 (PATH) |
| `ld: cannot find -lcudart / -lcuda` -> `Ninja build failed` -> `Engine core initialization failed` | FlashInfer mnnvl allreduce can't link CUDA | Fix 2 (cuda-link + LIBRARY_PATH) |
| `ld: cannot find -lnvrtc` -> `Ninja build failed` on `fp8_blockscale_gemm_90` -> `Engine core initialization failed` | FP8 model (GLM-5.2) FlashInfer kernel needs NVRTC, and FlashInfer's `-L` points at a non-existent `$CUDA_HOME/lib64` | Fix 3 (libnvrtc.so + populate `$CUDA_HOME/lib64`) |

**Model -> which fixes:** Kimi-K2.7-Code = Fix 1 + Fix 2. GLM-5.2-FP8 = Fix 1 + Fix 2 + Fix 3. Applying all three is harmless for any model, so the combined launch block above is a safe default on this node.
