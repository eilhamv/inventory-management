"""
Tests for task API endpoints.
"""
import pytest

import main


@pytest.fixture(autouse=True)
def clear_tasks():
    """Tasks are module-level state; isolate each test."""
    main.tasks.clear()
    main.task_sequence = 0
    yield
    main.tasks.clear()
    main.task_sequence = 0


def create(client, title="Review Q4 inventory", priority="high", due="2025-10-20"):
    return client.post("/api/tasks", json={"title": title, "priority": priority, "dueDate": due})


class TestTaskEndpoints:
    """Test suite for task endpoints."""

    def test_get_tasks_empty(self, client):
        """Test that the task list starts empty."""
        response = client.get("/api/tasks")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_task(self, client):
        """Test creating a task."""
        response = create(client)
        assert response.status_code == 201

        task = response.json()
        assert task["id"] == "TASK-1"
        assert task["title"] == "Review Q4 inventory"
        assert task["priority"] == "high"
        assert task["dueDate"] == "2025-10-20"
        assert task["status"] == "pending"

    def test_task_ids_are_strings(self, client):
        """Test that IDs never collide with the UI's integer mock task IDs."""
        task = create(client).json()
        assert isinstance(task["id"], str)
        assert not task["id"].isdigit()

    def test_get_tasks_newest_first(self, client):
        """Test that tasks are listed newest first, as the UI adds them."""
        create(client, title="first")
        create(client, title="second")

        titles = [t["title"] for t in client.get("/api/tasks").json()]
        assert titles == ["second", "first"]

    def test_toggle_task(self, client):
        """Test toggling a task to completed and back."""
        task_id = create(client).json()["id"]

        response = client.patch(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

        assert client.patch(f"/api/tasks/{task_id}").json()["status"] == "pending"

    def test_delete_task(self, client):
        """Test deleting a task."""
        task_id = create(client).json()["id"]

        response = client.delete(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        assert response.json()["id"] == task_id
        assert client.get("/api/tasks").json() == []

    def test_toggle_nonexistent_task(self, client):
        """Test toggling a task that doesn't exist."""
        response = client.patch("/api/tasks/TASK-999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_delete_nonexistent_task(self, client):
        """Test deleting a task that doesn't exist."""
        response = client.delete("/api/tasks/TASK-999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_default_priority(self, client):
        """Test that priority defaults to medium."""
        response = client.post("/api/tasks", json={"title": "t", "dueDate": "2025-10-20"})
        assert response.status_code == 201
        assert response.json()["priority"] == "medium"

    def test_title_is_trimmed(self, client):
        """Test that surrounding whitespace is removed from titles."""
        assert create(client, title="  padded  ").json()["title"] == "padded"

    def test_blank_title_rejected(self, client):
        """Test that whitespace-only titles are rejected without using an ID."""
        response = create(client, title="   ")
        assert response.status_code == 400
        assert create(client).json()["id"] == "TASK-1"

    def test_invalid_input_rejected(self, client):
        """Test validation of empty title, unknown priority and bad dates."""
        assert create(client, title="").status_code == 422
        assert create(client, priority="urgent").status_code == 422
        assert create(client, due="next week").status_code == 422
        assert client.post("/api/tasks", json={"title": "t"}).status_code == 422
