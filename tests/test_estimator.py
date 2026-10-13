from cost_engine.estimator import estimate_monthly_waste

def test_estimate_monthly_waste():
    resources = [
        {"type": "EBS", "region": "us-east-1", "id": "vol-1", "size_gb": 50},
        {"type": "EC2", "region": "us-east-1", "id": "i-1", "instance_type": "t3.micro"},
        {"type": "ELB", "region": "us-east-1", "id": "lb-1"},
        {"type": "RDS", "region": "us-east-1", "id": "db-1"},
    ]

    estimated, total = estimate_monthly_waste(resources)

    assert len(estimated) == 4
    assert total > 0
    assert estimated[0]["monthly_cost"] == 5.0  # 50GB * $0.10
