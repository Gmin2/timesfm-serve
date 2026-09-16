# AWS pilot evidence

The AWS deployment was torn down on 2026-09-16 after the project moved to IEX price
forecasting, which runs entirely on a laptop. These two records are the parts that
existed only in AWS and could not be regenerated afterwards.

- `deployment-verification.json`: the dated machine-readable record of the September
  2026 deployment, listing image digests, model provenance and the three real
  HTTPS/CUDA replays that were verified against saved references.
- `report.json`: the one output-head fine-tuning run that actually executed on the
  cluster GPU. It is a four-step engineering smoke run, correctly rejected by the
  review gate, kept because it is the only evidence the single-GPU train/serve
  handoff ran on real hardware.

Everything else in the pilot is reproducible from `infra/` and `deploy/`: the model
artifact came from Hugging Face, the replay archive is in `results/weather/`, and
the build zips were content-addressed copies of this repository.
