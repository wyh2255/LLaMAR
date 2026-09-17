"""Base class for LLM clients."""

from abc import ABC, abstractmethod
from typing import Any

from ..retry import RetryConfig
from ..schema import LLMResponse, Message


class MalformedLLMResponseError(Exception):
    """The LLM response could not be parsed within the retry budget.

    Providers intermittently return malformed tool-call arguments (truncated
    payloads, missing delimiters, empty strings). The client already retried the
    same request up to ``malformed_json_retries`` times; when it still fails it
    raises this typed error so the Agent loop can degrade that round instead of
    aborting the whole run on a single bad response.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        last_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        #: Number of API attempts spent before giving up (1 + retries).
        self.attempts = attempts
        #: Underlying parse error (json.JSONDecodeError / ValidationError).
        self.last_error = last_error


class LLMClientBase(ABC):
    """Abstract base class for LLM clients.

    This class defines the interface that all LLM clients must implement,
    regardless of the underlying API protocol (Anthropic, OpenAI, etc.).
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        retry_config: RetryConfig | None = None,
    ):
        """Initialize the LLM client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.retry_config = retry_config or RetryConfig()

        # Callback for tracking retry count
        self.retry_callback = None

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content, thinking, and tool calls
        """
        pass

    @abstractmethod
    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request payload for the API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        pass

    @abstractmethod
    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal message format to API-specific format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        pass
