from utils.aws_helpers import chunk_list, get_region_from_az, safe_get_first


class TestGetRegionFromAz:
    """Test region extraction from availability zones."""

    def test_standard_az(self):
        """Test standard AZ format."""
        assert get_region_from_az("us-east-1a") == "us-east-1"
        assert get_region_from_az("eu-west-2b") == "eu-west-2"
        assert get_region_from_az("ap-southeast-1c") == "ap-southeast-1"

    def test_local_zone(self):
        """Test Local Zone format."""
        assert get_region_from_az("us-east-1-bos-1a") == "us-east-1"
        assert get_region_from_az("us-west-2-lax-1a") == "us-west-2"
        assert get_region_from_az("eu-central-1-ham-1a") == "eu-central-1"

    def test_wavelength_zone(self):
        """Test Wavelength Zone format."""
        assert get_region_from_az("us-east-1-wl1-bos-wlz-1") == "us-east-1"
        assert get_region_from_az("us-west-2-wl1-sfo-wlz-1") == "us-west-2"

    def test_govcloud_zone(self):
        """Test GovCloud AZ format."""
        assert get_region_from_az("us-gov-west-1a") == "us-gov-west-1"
        assert get_region_from_az("us-gov-east-1b") == "us-gov-east-1"

    def test_iso_zone(self):
        """Test ISO (us-iso / us-isob / cn) AZ formats."""
        assert get_region_from_az("us-iso-east-1a") == "us-iso-east-1"
        assert get_region_from_az("us-isob-east-1a") == "us-isob-east-1"
        assert get_region_from_az("cn-north-1a") == "cn-north-1"
        assert get_region_from_az("cn-northwest-1b") == "cn-northwest-1"

    def test_outpost_zone(self):
        """Test Outposts-style AZ format."""
        assert get_region_from_az("us-east-1a-123456789012") == "us-east-1"

    def test_empty_az(self):
        """Test empty/None AZ."""
        assert get_region_from_az(None) is None
        assert get_region_from_az("") is None

    def test_malformed_az(self):
        """Test malformed AZ (short string) fail-open to None."""
        # Malformed input must not propagate as a region (would cause InvalidEndpoint).
        assert get_region_from_az("us") is None
        assert get_region_from_az("us-east") is None
        assert get_region_from_az(123) is None
        assert get_region_from_az("not-an-az-at-all-xyz") is None

    def test_multiple_standard_azs(self):
        """Test multiple standard AZ formats."""
        assert get_region_from_az("us-east-1a") == "us-east-1"
        assert get_region_from_az("us-east-1b") == "us-east-1"
        assert get_region_from_az("us-east-1c") == "us-east-1"
        assert get_region_from_az("eu-west-2a") == "eu-west-2"
        assert get_region_from_az("ap-southeast-1b") == "ap-southeast-1"


class TestSafeGetFirst:
    """Test safe list access."""

    def test_non_empty_list(self):
        """Test getting first element from non-empty list."""
        assert safe_get_first([1, 2, 3]) == 1
        assert safe_get_first(["a", "b"]) == "a"
        assert safe_get_first([{"key": "value"}]) == {"key": "value"}

    def test_empty_list(self):
        """Test getting from empty list returns default."""
        assert safe_get_first([]) is None
        assert safe_get_first([], default="default") == "default"
        assert safe_get_first([], default=0) == 0

    def test_single_element(self):
        """Test list with single element."""
        assert safe_get_first([42]) == 42


class TestChunkList:
    """Test list chunking."""

    def test_even_chunks(self):
        """Test chunking with evenly divisible size."""
        items = list(range(10))
        chunks = list(chunk_list(items, 5))

        assert len(chunks) == 2
        assert chunks[0] == [0, 1, 2, 3, 4]
        assert chunks[1] == [5, 6, 7, 8, 9]

    def test_uneven_chunks(self):
        """Test chunking with remainder."""
        items = list(range(10))
        chunks = list(chunk_list(items, 3))

        assert len(chunks) == 4
        assert chunks[0] == [0, 1, 2]
        assert chunks[1] == [3, 4, 5]
        assert chunks[2] == [6, 7, 8]
        assert chunks[3] == [9]

    def test_single_chunk(self):
        """Test when chunk size is larger than list."""
        items = [1, 2, 3]
        chunks = list(chunk_list(items, 10))

        assert len(chunks) == 1
        assert chunks[0] == [1, 2, 3]

    def test_empty_list(self):
        """Test chunking empty list."""
        chunks = list(chunk_list([], 5))
        assert len(chunks) == 0

    def test_chunk_size_one(self):
        """Test chunking with size 1."""
        items = [1, 2, 3]
        chunks = list(chunk_list(items, 1))

        assert len(chunks) == 3
        assert chunks[0] == [1]
        assert chunks[1] == [2]
        assert chunks[2] == [3]

    def test_chunk_size_twenty(self):
        """Test batching 25 items into chunks of 20 (AWS limit)."""
        items = list(range(25))
        chunks = list(chunk_list(items, 20))

        assert len(chunks) == 2
        assert len(chunks[0]) == 20
        assert len(chunks[1]) == 5
