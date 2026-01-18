from cost_engine.estimator import estimate_monthly_waste

def test_estimate_monthly_waste():
    resources = [
        {"type": "EBS", "id": "vol-1", "size_gb": 50},
        {"type": "EC2", "id": "i-1", "instance_type": "t3.micro"},
        {"type": "ELB", "id": "lb-1"},
        {"type": "RDS", "id": "db-1"},
    ]

    estimated, total = estimate_monthly_waste(resources)

    assert len(estimated) == 4
    assert total > 0
    assert estimated[0]["monthly_cost"] == 5.0  # 50GB * $0.10
