# dexim-manus-brainco

Development workspace for the DexImitate Manus Glove → BrainCo Revo2 Hand teleoperation system.

## Setup

```bash
pixi install
```

This builds all packages including `manus-sdk-python` (C++ extension with default SDK 3.1.1).
The Manus SDK version can be overridden by setting the `MANUS_SDK_VERSION` environment variable
before running `pixi install`, e.g. `$env:MANUS_SDK_VERSION = "3.1.1"`.

### Hardware environment

For the hardware (`hw`) environment (includes RealSense with `[hw]` extras and BrainCo hardware SDK):

```bash
pixi install -e hw
```

## PyTorch (GPU / CPU)

The default environment installs **`pytorch-cpu`** from conda-forge.

### CPU-only machines

If you are on a machine without a CUDA-capable GPU, the default configuration works out of the box.

### GPU machines

If you have an NVIDIA GPU with CUDA 13.0 drivers, swap the pytorch dependency in `pixi.toml`:

```toml
[dependencies]
pytorch-gpu = ">=2.2.1,<2.11.0"
cuda-version = ">=13,<14"
```

Then re-run `pixi install`.

## Quick Start

```bash
# List available sessions
dexim launch

# Start single-hand teleop (mock mode — no hardware needed)
dexim launch --session single-hand-teleop-mock

# Hardware mode
dexim launch --session single-hand-teleop
```
