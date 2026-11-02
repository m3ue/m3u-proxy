# Testing Guide for m3u-proxy

This document explains the testing setup and best practices for the m3u-proxy project.

## Testing Structure

```
m3u-proxy/
├── tests/
│   ├── __init__.py
│   ├── test_api.py              # API endpoint tests
│   ├── test_stream_manager.py   # Core functionality tests
│   └── integration/
│       ├── __init__.py
│       └── test_integration.py  # End-to-end tests
├── pytest.ini                   # pytest configuration
├── run_tests.py                 # Test runner script
├── requirements.txt             # runtime dependencies (installed in the image)
└── requirements-dev.txt         # runtime + testing dependencies
```

## Testing Frameworks Used

- **pytest**: Main testing framework (most popular in Python)
- **pytest-asyncio**: For testing async/await code
- **pytest-httpx**: For mocking HTTP requests
- **pytest-cov**: For code coverage reports

## Types of Tests

### 1. Unit Tests
Test individual components in isolation:
- `test_stream_manager.py`: Tests StreamManager, ClientInfo, StreamInfo classes
- `test_api.py`: Tests FastAPI endpoints and helper functions

### 2. Integration Tests
Test how components work together:
- `test_integration.py`: Full end-to-end workflows

## Running Tests

### Basic Usage
```bash
# Run all tests
python run_tests.py

# Or directly with pytest
python -m pytest

# Run specific test file
python -m pytest tests/test_api.py

# Run specific test method
python -m pytest tests/test_api.py::TestAPI::test_create_stream_post_valid
```

### Test Categories
```bash
# Run only unit tests
python run_tests.py --unit

# Run only integration tests  
python run_tests.py --integration

# Run with coverage report
python run_tests.py --coverage

# Verbose output
python run_tests.py --verbose
```

### Advanced pytest Usage
```bash
# Run tests matching pattern
python -m pytest -k "test_create"

# Stop on first failure
python -m pytest -x

# Run in parallel (requires pytest-xdist)
python -m pytest -n 4

# Show local variables on failure
python -m pytest -l

# Run only failed tests from last run
python -m pytest --lf
```

## Test Writing Guidelines

### 1. Test Naming
- Test files: `test_*.py`
- Test classes: `Test*`
- Test methods: `test_*`
- Be descriptive: `test_create_stream_with_failover_urls`

### 2. Test Structure (AAA Pattern)
```python
def test_something(self):
    # Arrange - set up test data
    url = "http://example.com/stream.m3u8"
    
    # Act - perform the action
    result = create_stream(url)
    
    # Assert - verify the result
    assert result is not None
    assert result.url == url
```

### 3. Using Fixtures
```python
@pytest.fixture
def stream_manager(self):
    return StreamManager()

def test_something(self, stream_manager):
    # Use the fixture
    result = stream_manager.create_stream("http://test.com")
    assert result is not None
```

### 4. Async Testing
```python
@pytest.mark.asyncio
async def test_async_function(self):
    result = await async_function()
    assert result == expected_value
```

### 5. Mocking External Dependencies
```python
from unittest.mock import Mock, AsyncMock, patch

@patch('httpx.AsyncClient')
def test_http_request(self, mock_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_client.return_value.get.return_value = mock_response
    
    # Test code that uses httpx
```

## Test Coverage

### Running Coverage
```bash
# Generate coverage report
python run_tests.py --coverage

# View HTML report
open htmlcov/index.html
```

### Coverage Goals
- **Unit Tests**: Aim for 90%+ coverage of core business logic
- **Integration Tests**: Cover main user workflows
- **Edge Cases**: Test error conditions and boundary cases

## Continuous Integration

### GitHub Actions Example
```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v2
    - name: Set up Python
      uses: actions/setup-python@v2
      with:
        python-version: 3.9
    - name: Install dependencies
      run: |
        pip install -r requirements-dev.txt
    - name: Run tests
      run: python run_tests.py --coverage
```

## Best Practices

### 1. Test Independence
- Each test should be independent
- Don't rely on test execution order
- Clean up after each test

### 2. Mock External Services
- Always mock HTTP requests to external services
- Use consistent mock data
- Test both success and failure scenarios

### 3. Test Data
```python
# Good - descriptive test data
def test_m3u8_parsing(self):
    playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:10.0,
segment1.ts"""
    
# Avoid - magic values
def test_something(self):
    result = function(42, "abc", True)  # What do these mean?
```

### 4. Error Testing
```python
def test_invalid_input_raises_error(self):
    with pytest.raises(ValueError, match="Invalid URL"):
        create_stream("not_a_url")
```

### 5. Parameterized Tests
```python
@pytest.mark.parametrize("url,expected", [
    ("test.m3u8", "application/vnd.apple.mpegurl"),
    ("video.mp4", "video/mp4"),
    ("stream.ts", "video/mp2t"),
])
def test_content_type_detection(self, url, expected):
    assert get_content_type(url) == expected
```

## Debugging Tests

### 1. Print Debugging
```python
def test_something(self):
    result = function()
    print(f"Debug: result = {result}")  # Use pytest -s to see output
    assert result == expected
```

### 2. Breakpoint Debugging
```python
def test_something(self):
    result = function()
    import pdb; pdb.set_trace()  # Debugger will stop here
    assert result == expected
```

### 3. Pytest Debugging Options
```bash
# Show print statements
python -m pytest -s

# Stop on first failure with traceback
python -m pytest -x --tb=long

# Show local variables in traceback
python -m pytest --tb=long -l
```

## Common Patterns

### Testing FastAPI Endpoints
```python
from fastapi.testclient import TestClient

def test_api_endpoint(self):
    client = TestClient(app)
    response = client.post("/streams", json={"url": "http://test.com"})
    assert response.status_code == 200
    assert "stream_id" in response.json()
```

### Testing Streaming Responses
```python
@pytest.mark.asyncio
async def test_streaming_response(self):
    async def mock_generator():
        yield b"chunk1"
        yield b"chunk2"
    
    chunks = []
    async for chunk in mock_generator():
        chunks.append(chunk)
    
    assert chunks == [b"chunk1", b"chunk2"]
```

### Testing with Real HTTP Calls (Sparingly)
```python
@pytest.mark.integration
@pytest.mark.slow
def test_real_http_call(self):
    # Only use for critical integration tests
    # Mark as slow so they can be skipped during development
    pass
```

## Performance Testing

For performance testing, consider:
- `pytest-benchmark` for microbenchmarks
- Load testing tools like `locust` for API endpoints
- Memory profiling with `pytest-memray`

## Documentation Testing

Test your documentation examples:
```python
def test_readme_example(self):
    # Copy example from README and verify it works
    pass
```

This testing setup provides comprehensive coverage for your IPTV proxy application with proper separation of concerns, good mocking practices, and clear organization.
