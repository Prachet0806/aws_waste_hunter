import json

from cost_engine.estimator import estimate_monthly_waste


def test_pricing_json_override(monkeypatch):
    monkeypatch.setenv("PRICING_JSON", json.dumps({"EBS": 1, "EC2": {"t3.micro": 99}, "ELB": 2, "RDS": 3}))
    resources = [{"type": "EBS", "id": "vol-1", "size_gb": 2}]

    estimated, total = estimate_monthly_waste(resources)

    assert total == 2
    assert estimated[0]["monthly_cost"] == 2


def test_pricing_file_override(monkeypatch, tmp_path):
    pricing = {"EBS": 0.5, "EC2": {"t3.micro": 10}, "ELB": 2, "RDS": 3}
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(pricing), encoding="utf-8")
    monkeypatch.delenv("PRICING_JSON", raising=False)
    monkeypatch.setenv("PRICING_FILE", str(path))

    resources = [{"type": "EBS", "id": "vol-1", "size_gb": 2}]
    estimated, total = estimate_monthly_waste(resources)

    assert total == 1
    assert estimated[0]["monthly_cost"] == 1
