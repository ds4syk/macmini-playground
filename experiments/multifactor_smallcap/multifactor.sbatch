#!/bin/bash
#SBATCH -A users
#SBATCH -J mf-smallcap
#SBATCH -p cpu
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=24G
#SBATCH -t 02:00:00
#SBATCH -o /mnt/storage1/syk/experiments/Share/multifactor/logs/job_%j.log
#SBATCH -e /mnt/storage1/syk/experiments/Share/multifactor/logs/job_%j.log
set -euo pipefail

ROOT=/home/syk/experiments/Share/multifactor
export MF_ROOT=/mnt/storage1/syk/experiments/Share/multifactor
export PYTHONPATH="$ROOT"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=8
export OPENBLAS_NUM_THREADS=8
export MPLCONFIGDIR="$MF_ROOT/runtime/matplotlib"
mkdir -p "$MF_ROOT/logs" "$MF_ROOT/runtime/matplotlib" "$MF_ROOT/results"
cd "$ROOT"

PY=/home/syk/experiments/Share/gru/runtime/venv/bin/python
$PY -u -m experiments.multifactor_smallcap.prepare
$PY -u -m experiments.multifactor_smallcap.factors
$PY -u -m experiments.multifactor_smallcap.train
$PY -u -m experiments.multifactor_smallcap.export_local
