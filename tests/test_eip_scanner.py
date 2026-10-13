from types import SimpleNamespace
import scanner.eip_scanner as eip_scanner


class FakeEC2Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages


class FakeEC2Client:
    def __init__(self, addresses_pages):
        self._region = "us-east-1"
        self.meta = SimpleNamespace(region_name=self._region)
        self.addresses_pages = addresses_pages

    def describe_addresses(self):
        return {"Addresses": [a for page in self.addresses_pages for a in page["Addresses"]]}


def _eip(**kwargs):
    base = {"PublicIp": "1.2.3.4", "Domain": "vpc"}
    base.update(kwargs)
    return base


def test_scan_unattached_eips_basic(monkeypatch):
    """Test basic EIP scan finds unattached addresses."""
    pages = [{
        "Addresses": [
            _eip(AllocationId="eipalloc-1", Tags=[{"Key": "owner", "Value": "sre"}]),
        ]
    }]

    fake = FakeEC2Client(pages)
    monkeypatch.setattr(eip_scanner, "_ec2_client", fake)

    results = eip_scanner.scan_unattached_eips()

    assert len(results) == 1
    assert results[0]["type"] == "EIP"
    assert results[0]["id"] == "eipalloc-1"
    assert results[0]["public_ip"] == "1.2.3.4"
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "sre"


def test_scan_unattached_eips_skips_associated(monkeypatch):
    """Test that associated EIPs are skipped."""
    pages = [{
        "Addresses": [
            _eip(AllocationId="eipalloc-assoc", AssociationId="eipassoc-1"),
            _eip(AllocationId="eipalloc-inst", InstanceId="i-123"),
            _eip(AllocationId="eipalloc-nic", NetworkInterfaceId="eni-123"),
            _eip(AllocationId="eipalloc-free"),
        ]
    }]

    fake = FakeEC2Client(pages)
    monkeypatch.setattr(eip_scanner, "_ec2_client", fake)

    results = eip_scanner.scan_unattached_eips()

    assert len(results) == 1
    assert results[0]["id"] == "eipalloc-free"


def test_scan_unattached_eips_fallback_id(monkeypatch):
    """Test EIP without AllocationId falls back to namespaced PublicIp."""
    pages = [{"Addresses": [_eip(AllocationId=None)]}]

    fake = FakeEC2Client(pages)
    monkeypatch.setattr(eip_scanner, "_ec2_client", fake)

    results = eip_scanner.scan_unattached_eips()

    assert len(results) == 1
    assert results[0]["id"] == "public-ip/1.2.3.4"
    assert results[0]["public_ip"] == "1.2.3.4"


def test_scan_unattached_eips_no_tags(monkeypatch):
    """Test EIP without Tags field."""
    pages = [{"Addresses": [_eip(AllocationId="eipalloc-1")]}]

    fake = FakeEC2Client(pages)
    monkeypatch.setattr(eip_scanner, "_ec2_client", fake)

    results = eip_scanner.scan_unattached_eips()

    assert results[0]["tags"] == {}


def test_scan_unattached_eips_empty(monkeypatch):
    """Test no addresses returns empty list."""
    fake = FakeEC2Client([{"Addresses": []}])
    monkeypatch.setattr(eip_scanner, "_ec2_client", fake)

    results = eip_scanner.scan_unattached_eips()

    assert results == []


def test_scan_unattached_eips_multiple_addresses(monkeypatch):
    """Test EIP scan across multiple pages."""
    pages = [
        {"Addresses": [_eip(AllocationId="eipalloc-1")]},
        {"Addresses": [_eip(AllocationId="eipalloc-2")]},
    ]

    fake = FakeEC2Client(pages)
    monkeypatch.setattr(eip_scanner, "_ec2_client", fake)

    results = eip_scanner.scan_unattached_eips()

    assert len(results) == 2


def test_scan_unattached_eips_error_propagates(monkeypatch):
    """Test that API errors propagate to caller."""
    class ExplodingClient:
        def __init__(self):
            self.meta = SimpleNamespace(region_name="us-east-1")

        def describe_addresses(self):
            raise RuntimeError("API down")

    monkeypatch.setattr(eip_scanner, "_ec2_client", ExplodingClient())

    try:
        eip_scanner.scan_unattached_eips()
        assert False, "expected exception"
    except RuntimeError:
        pass