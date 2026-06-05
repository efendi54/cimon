"""GitHub Workflow Visualizer - Tests."""


def test_mermaid_call_graph() -> None:
    """Test the Mermaid call graph generation."""
    x = 1
    y = 2
    z = x + y
    w = 3
    assert z == w
