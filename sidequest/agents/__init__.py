"""sidequest.agents — Claude CLI agent foundation.

Port of sidequest-agents crate (Phase 1 subset).
ADR-082: Python server narration vertical slice.

Phase 1 exports:
- Agent (Protocol), AgentResponse, BaseAgent, make_agent
- ClaudeClient, LlmClient, ClaudeClientBuilder, ClaudeResponse
- ClaudeClientError, TimeoutError, SubprocessFailed, EmptyResponse
- PromptComposer, PromptRegistry, PromptSection
- AttentionZone, SectionCategory, RuleTier
- SoulData, SoulPrinciple, parse_soul_md
- NarratorAgent (Phase 1 — story 41-5)
- Orchestrator, TurnContext, NarrationTurnResult (Phase 1 — story 41-5)
- ActionRewrite, BeatSelection, VisualScene, NpcMention (Phase 1 — story 41-5)
"""

from __future__ import annotations

from sidequest.agents.agent import Agent, AgentResponse, BaseAgent, make_agent
from sidequest.agents.anthropic_sdk_client import (
    AnthropicSdkClient,
    AnthropicSdkClientError,
    AnthropicSdkConfigError,
    AnthropicSdkLoopExceeded,
)
from sidequest.agents.claude_client import (
    ClaudeClient,
    ClaudeClientBuilder,
    ClaudeClientError,
    ClaudeResponse,
    EmptyResponse,
    LlmCapabilities,
    LlmClient,
    LlmClientError,
    SubprocessFailed,
)
from sidequest.agents.claude_client import TimeoutError as ClaudeTimeoutError
from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure
from sidequest.agents.llm_factory import UnknownBackend, build_llm_client
from sidequest.agents.model_routing import CallType, resolve_model
from sidequest.agents.narrator import NarratorAgent, narrator_output_format_text
from sidequest.agents.ollama_client import OllamaClient, OllamaClientError
from sidequest.agents.orchestrator import (
    ActionRewrite,
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
    Orchestrator,
    TurnContext,
    VisualScene,
    extract_structured_from_response,
)
from sidequest.agents.perception_filter import NoopPerceptionFilter, PerceptionFilter
from sidequest.agents.prompt_framework import (
    AttentionZone,
    PromptComposer,
    PromptRegistry,
    PromptSection,
    RuleTier,
    SectionCategory,
    SoulData,
    SoulPrinciple,
    parse_soul_md,
)
from sidequest.agents.tool_registry import (
    Registry,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
    tool,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolingLlmClient,
    ToolingResult,
    ToolResultBlock,
    ToolUseBlock,
)

__all__ = [
    # intent router producer (ADR-113, Story 59-2)
    "IntentRouter",
    "IntentRouterFailure",
    # narrator + orchestrator (Phase 1 — story 41-5)
    "NarratorAgent",
    "narrator_output_format_text",
    "ActionRewrite",
    "BeatSelection",
    "NarrationTurnResult",
    "NpcMention",
    "Orchestrator",
    "TurnContext",
    "VisualScene",
    "extract_structured_from_response",
    # agent
    "Agent",
    "AgentResponse",
    "BaseAgent",
    "make_agent",
    # claude_client
    "ClaudeClient",
    "ClaudeClientBuilder",
    "ClaudeClientError",
    "ClaudeResponse",
    "ClaudeTimeoutError",
    "EmptyResponse",
    "LlmCapabilities",
    "LlmClient",
    "LlmClientError",
    "SubprocessFailed",
    # prompt_framework
    "AttentionZone",
    "PromptComposer",
    "PromptRegistry",
    "PromptSection",
    "RuleTier",
    "SectionCategory",
    "SoulData",
    "SoulPrinciple",
    "parse_soul_md",
]

__all__ += [
    "OllamaClient",
    "OllamaClientError",
    "UnknownBackend",
    "build_llm_client",
    # anthropic_sdk_client
    "AnthropicSdkClient",
    "AnthropicSdkClientError",
    "AnthropicSdkConfigError",
    "AnthropicSdkLoopExceeded",
    # model_routing
    "CallType",
    "resolve_model",
    # tooling_protocol
    "CacheableBlock",
    "Message",
    "ToolDefinition",
    "ToolingLlmClient",
    "ToolingResult",
    "ToolResultBlock",
    "ToolUseBlock",
    # perception_filter (Phase B)
    "NoopPerceptionFilter",
    "PerceptionFilter",
    # tool_registry (Phase B)
    "Registry",
    "ToolCategory",
    "ToolContext",
    "ToolResult",
    "ToolResultStatus",
    "default_registry",
    "tool",
]
