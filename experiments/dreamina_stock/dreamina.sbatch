#!/bin/bash
#SBATCH -A users
#SBATCH -J dreamina-stock
#SBATCH -p gpu
#SBATCH --ntasks=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH -t 01:00:00
#SBATCH -o /mnt/storage1/syk/experiments/Share/dreamina/logs/job_%j.log
#SBATCH -e /mnt/storage1/syk/experiments/Share/dreamina/logs/job_%j.log
set -euo pipefail

CODE=/home/syk/experiments/Share/dreamina
OUT=/mnt/storage1/syk/experiments/Share/dreamina
cd "$CODE"

export VLA_PANEL_DIR=/mnt/storage1/syk/experiments/Share/gru/data
export VLA_CODE_DIR=/home/syk/experiments/Share/v2
export PYTHONPATH="$CODE"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4
export MPLCONFIGDIR="$OUT/runtime/matplotlib"
export CUDA_CACHE_PATH="$OUT/runtime/cuda-cache"
export CUBLAS_WORKSPACE_CONFIG=:4096:8

CONFIG="${CONFIG:-config.json}"
OUTDIR="${OUTDIR:-results}"
PY=/home/syk/experiments/Share/gru/runtime/venv/bin/python
$PY -u -m experiments.dreamina_stock.train \
  --config "experiments/dreamina_stock/$CONFIG" \
  --output "$OUT/$OUTDIR"
$PY -u -m experiments.dreamina_stock.analyze --reports "$OUT/$OUTDIR"
