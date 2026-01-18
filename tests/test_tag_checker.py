from compliance.tag_checker import check_tag_compliance

def test_tag_compliance():
    resources = [
        {"type": "EC2", "id": "i-1", "tags": {"owner": "sre", "env": "prod"}},
        {"type": "EBS", "id": "vol-1", "tags": {"env": "prod"}},
    ]

    violations = check_tag_compliance(resources)

    assert len(violations) == 2
    assert violations[0]["resource_id"] == "i-1"
    assert "cost-center" in violations[0]["missing_tags"]
    assert "owner" in violations[1]["missing_tags"]
