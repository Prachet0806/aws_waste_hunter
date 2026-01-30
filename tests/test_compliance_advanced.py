import pytest
from compliance import tag_checker


class TestRequiredTagsConfiguration:
    """Test configurable required tags."""

    def test_default_required_tags(self, monkeypatch):
        """Test default required tags."""
        monkeypatch.delenv("REQUIRED_TAGS", raising=False)
        
        # Reset cached tags
        tags = tag_checker._get_required_tags()
        
        assert "owner" in tags
        assert "env" in tags
        assert "cost-center" in tags

    def test_custom_required_tags(self, monkeypatch):
        """Test custom required tags from env var."""
        monkeypatch.setenv("REQUIRED_TAGS", "team,project,environment")
        
        tags = tag_checker._get_required_tags()
        
        assert "team" in tags
        assert "project" in tags
        assert "environment" in tags
        assert "owner" not in tags

    def test_required_tags_with_spaces(self, monkeypatch):
        """Test tags with spaces are trimmed."""
        monkeypatch.setenv("REQUIRED_TAGS", " owner , env , cost-center ")
        
        tags = tag_checker._get_required_tags()
        
        assert tags == ["owner", "env", "cost-center"]


class TestTagComplianceChecking:
    """Test tag compliance checking logic."""

    def test_compliant_resources(self, monkeypatch):
        """Test resources with all required tags."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner,env")
        
        resources = [
            {
                "type": "EBS",
                "id": "vol-1",
                "tags": {"owner": "sre", "env": "prod", "extra": "value"}
            }
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 0

    def test_missing_single_tag(self, monkeypatch):
        """Test resource missing one required tag."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner,env,cost-center")
        
        resources = [
            {
                "type": "EC2",
                "id": "i-1",
                "tags": {"owner": "sre", "env": "prod"}
            }
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 1
        assert violations[0]["resource_id"] == "i-1"
        assert "cost-center" in violations[0]["missing_tags"]
        assert "owner" not in violations[0]["missing_tags"]

    def test_missing_all_tags(self, monkeypatch):
        """Test resource with no tags."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner,env")
        
        resources = [
            {
                "type": "EBS",
                "id": "vol-1",
                "tags": {}
            }
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 1
        assert set(violations[0]["missing_tags"]) == {"owner", "env"}

    def test_multiple_violations(self, monkeypatch):
        """Test multiple resources with violations."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner,env")
        
        resources = [
            {"type": "EBS", "id": "vol-1", "tags": {"owner": "sre"}},  # Missing env
            {"type": "EC2", "id": "i-1", "tags": {"env": "prod"}},  # Missing owner
            {"type": "ELB", "id": "lb-1", "tags": {}},  # Missing both
            {"type": "RDS", "id": "db-1", "tags": {"owner": "dba", "env": "staging"}},  # OK
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 3
        ids = [v["resource_id"] for v in violations]
        assert "vol-1" in ids
        assert "i-1" in ids
        assert "lb-1" in ids
        assert "db-1" not in ids

    def test_no_tags_field(self, monkeypatch):
        """Test resource without tags field."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner")
        
        resources = [
            {"type": "EBS", "id": "vol-1"}  # No tags field
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 1
        assert "owner" in violations[0]["missing_tags"]

    def test_empty_resources(self, monkeypatch):
        """Test with no resources."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner,env")
        
        resources = []

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 0

    def test_violation_details(self, monkeypatch):
        """Test violation details are complete."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner,env,cost-center")
        
        resources = [
            {
                "type": "EC2",
                "id": "i-test",
                "tags": {"owner": "john", "extra": "tag"}
            }
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 1
        v = violations[0]
        assert v["resource_id"] == "i-test"
        assert v["type"] == "EC2"
        assert set(v["missing_tags"]) == {"env", "cost-center"}
        assert v["tags"]["owner"] == "john"
        assert v["tags"]["extra"] == "tag"


class TestCaseInsensitivity:
    """Test case sensitivity in tag checking."""

    def test_case_sensitive_tags(self, monkeypatch):
        """Test that tag keys are case-sensitive."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner")
        
        resources = [
            {
                "type": "EBS",
                "id": "vol-1",
                "tags": {"Owner": "sre"}  # Capital O
            }
        ]

        violations = tag_checker.check_tag_compliance(resources)

        # Should be a violation (AWS tags are case-sensitive)
        assert len(violations) == 1
        assert "owner" in violations[0]["missing_tags"]


class TestEdgeCases:
    """Test edge cases in tag compliance."""

    def test_single_required_tag(self, monkeypatch):
        """Test with only one required tag."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner")
        
        resources = [
            {"type": "EBS", "id": "vol-1", "tags": {}},
            {"type": "EC2", "id": "i-1", "tags": {"owner": "sre"}},
        ]

        violations = tag_checker.check_tag_compliance(resources)

        assert len(violations) == 1
        assert violations[0]["resource_id"] == "vol-1"

    def test_empty_required_tags(self, monkeypatch):
        """Test with no required tags configured."""
        monkeypatch.setenv("REQUIRED_TAGS", "")
        
        resources = [
            {"type": "EBS", "id": "vol-1", "tags": {}},
        ]

        violations = tag_checker.check_tag_compliance(resources)

        # No required tags means no violations
        assert len(violations) == 0

    def test_tag_value_empty_string(self, monkeypatch):
        """Test that empty tag values still count as present."""
        monkeypatch.setenv("REQUIRED_TAGS", "owner")
        
        resources = [
            {
                "type": "EBS",
                "id": "vol-1",
                "tags": {"owner": ""}  # Empty value but key exists
            }
        ]

        violations = tag_checker.check_tag_compliance(resources)

        # Key exists, so no violation
        assert len(violations) == 0
