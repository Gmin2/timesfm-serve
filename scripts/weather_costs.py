"""Offline estimate from the dated AWS rate snapshot; never queries or changes AWS."""

import argparse
import json
from pathlib import Path


def estimate(hours=730, gpu_hours=0):
    if not 0 <= gpu_hours <= hours or hours <= 0:
        raise ValueError("Require 0 <= GPU hours <= stack hours and stack hours > 0")
    data = json.loads((Path(__file__).resolve().parents[1] / "infra/runtime/costs.json").read_text())
    rates = {k: v["usd"] for k, v in data["rates"].items()}
    items = {
        "eks": hours * rates["eks_standard_hour"],
        "two_standard_nodes": hours * 2 * rates["t3_medium_hour"],
        "rds_instance": hours * rates["rds_t4g_micro_hour"],
        "nat_gateway": hours * rates["nat_hour"],
        "alb_base": hours * rates["alb_hour"],
        "nat_public_ipv4_address": hours * rates["public_ipv4_hour"],
        "standard_node_disks_40gb": hours / 730 * 40 * rates["ebs_gp3_gb_month"],
        "rds_storage_20gb": hours / 730 * 20 * rates["rds_gp3_gb_month"],
        "gpu_instance": gpu_hours * rates["g6_xlarge_hour"],
        "gpu_disk_80gb": gpu_hours / 730 * 80 * rates["ebs_gp3_gb_month"],
    }
    return {
        "rate_date": data["checked_at"], "region": data["region"], "stack_hours": hours, "gpu_hours": gpu_hours,
        "usd": {k: round(v, 2) for k, v in items.items()}, "fixed_subtotal_usd": round(sum(items.values()), 2),
        "not_included": "API Gateway requests, CodeBuild minutes, ALB LCUs, NAT processing, Secrets Manager, S3/ECR storage and requests, logs, extra backups, cross-AZ/internet traffic, taxes; billing rounding and retained resources",
        "hard_spending_cap": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=730)
    parser.add_argument("--gpu-hours", type=float, default=0)
    args = parser.parse_args()
    print(json.dumps(estimate(args.hours, args.gpu_hours), indent=2))


if __name__ == "__main__":
    main()
