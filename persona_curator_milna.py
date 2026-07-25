"""
kilonova/tools/persona_curator.py

Final stage of GM Persona build pipeline. Synthesizes specialist outputs
(narrative, mechanics, philosophy) into a single routing document for
play-time LLM, with conflict resolution, validation, and fallback handling.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Domain weighting for conflict resolution
DOMAIN_WEIGHTS = {
    "narrative": {"narrative": 0.60, "philosophy": 0.30, "mechanics": 0.10},
    "mechanical": {"mechanics": 0.70, "narrative": 0.20, "philosophy": 0.10},
    "faction": {"philosophy": 0.50, "narrative": 0.35, "mechanics": 0.15},
}

# Curator system prompt — instructs LLM on synthesis task
CURATOR_SYSTEM = """You are a GM Persona Curator. Your task is to synthesize outputs from three specialist LLMs (narrative, mechanics, philosophy) into a single cohesive GM Persona routing document.

Your output MUST be valid JSON matching this schema:
{
  "meta": {
    "personaId": "string (e.g. gm-001)",
    "moduleId": "string or null",
    "personaVersion": "1.0.0",
    "createdAt": "ISO8601 timestamp",
    "specialists": ["array of specialist roles that succeeded"],
    "dotTopic": "string (the knowledge base topic)"
  },
  "coreInstructions": {
    "identity": "who the GM is (from narrative)",
    "authority": "where knowledge comes from (from DOT overview)",
    "failureMode": "what to do when uncertain (from philosophy)"
  },
  "voiceFramework": {
    "overallTone": "string (from narrative)",
    "sentenceStructure": "string (from narrative)",
    "pacing": {
      "exploration": "string",
      "combat": "string",
      "downtime": "string"
    },
    "avoid": ["array of things GM never does"]
  },
  "routingRules": [
    {
      "id": "rule-001",
      "trigger": "player input pattern that activates this rule",
      "dotPath": "relative/path/in/kb/topic/",
      "fallbackChain": ["fallback-rule-id-1", "fallback-rule-id-2"],
      "voiceOverride": "string or null",
      "domain": "narrative|mechanics|philosophy",
      "confidence": 0.85
    }
  ],
  "factionVoices": [
    {
      "factionId": "string",
      "factionName": "string",
      "tone": "string",
      "speechPatterns": ["array of speech pattern examples"],
      "example": "a full example line of dialogue"
    }
  ],
  "fallbackProtocol": {
    "routingFailure": "what to say when no rule matches",
    "dotMissing": "what to say when DOT path not found",
    "lowConfidence": "what to say when uncertain"
  },
  "curatorNotes": "string - conflicts resolved, decisions made, open issues"
}

When resolving conflicts between specialist outputs, apply these weights:
- Narrative decisions: narrative 60%, philosophy 30%, mechanics 10%
- Mechanical decisions: mechanics 70%, narrative 20%, philosophy 10%
- Faction decisions: philosophy 50%, narrative 35%, mechanics 15%

Preserve as much specialist detail as possible. Merge contradictions by weighted preference.
Return ONLY the JSON object, no markdown fence or extra text."""


def load_dot_content(kb_dir: Path, max_chars: int = 20000) -> str:
    """
    Load and combine DOT (Domain of Truth) content from knowledge base.
    
    Loads overview.md + first 10 raw/*.md files from kb_dir, truncates to max_chars.
    Returns combined string for curator context.
    
    Args:
        kb_dir: Path to knowledge base directory (e.g., kb/adventure-name/)
        max_chars: Maximum total characters to return (default 20000 to stay under token limit)
    
    Returns:
        Combined DOT content as string, or empty string if kb_dir missing
    """
    if not kb_dir.exists():
        logger.warning(f"DOT directory not found: {kb_dir}")
        return ""
    
    content_parts = []
    
    # Load overview.md if exists
    overview_path = kb_dir / "overview.md"
    if overview_path.exists():
        try:
            overview_text = overview_path.read_text(encoding="utf-8")
            content_parts.append(f"# DOT Overview\n\n{overview_text}")
        except Exception as e:
            logger.warning(f"Failed to load overview.md: {e}")
    
    # Load first 10 raw/*.md files
    raw_dir = kb_dir / "raw"
    if raw_dir.exists():
        raw_files = sorted(raw_dir.glob("*.md"))[:10]
        for raw_file in raw_files:
            try:
                raw_text = raw_file.read_text(encoding="utf-8")
                content_parts.append(f"## {raw_file.name}\n\n{raw_text}")
            except Exception as e:
                logger.warning(f"Failed to load {raw_file.name}: {e}")
    
    combined = "\n\n---\n\n".join(content_parts)
    
    # Truncate to max_chars if needed
    if len(combined) > max_chars:
        combined = combined[:max_chars] + f"\n\n[... truncated, {len(combined) - max_chars} chars omitted]"
    
    return combined


class PersonaCurator:
    """
    Synthesizes specialist outputs (narrative, mechanics, philosophy)
    into a single GM Persona routing document.
    
    Never raises — all failures return a stub persona with meta.status='stub'.
    """
    
    def __init__(self, engine):
        """
        Initialize curator with conversation engine.
        
        Args:
            engine: ConversationEngine instance with specialist_round() method
        """
        self.engine = engine
    
    async def synthesize(
        self,
        topic: str,
        specialist_outputs: dict[str, dict],
        dot_overview: str,
        kb_dir: Path,
        module_id: Optional[str] = None,
    ) -> dict:
        """
        Synthesize specialist outputs into a complete GM Persona.
        
        Fires curator LLM call on best available leg (anthropic > openai > gemini),
        validates routing rules against disk, and returns assembled persona dict.
        Never raises — returns stub on any failure.
        
        Args:
            topic: Knowledge base topic name (e.g., "lost-temple")
            specialist_outputs: Output from engine.specialist_round(), keyed by
                               'narrative', 'mechanics', 'philosophy'
            dot_overview: String overview of domain of truth content
            kb_dir: Path to knowledge base directory (e.g., kb/lost-temple/)
            module_id: Optional module identifier to stamp on persona
        
        Returns:
            Complete persona dict with meta, coreInstructions, voiceFramework,
            routingRules, factionVoices, fallbackProtocol, curatorNotes.
            On failure: stub persona with meta.status='stub' and meta.error set.
        """
        try:
            # Filter to successful specialist outputs
            successful = {
                role: output
                for role, output in specialist_outputs.items()
                if output.get("ok") and output.get("artifact") is not None
            }
            
            if not successful:
                error_msg = "No successful specialist outputs"
                logger.error(error_msg)
                return self._stub_persona(topic, module_id, error_msg)
            
            # Build curator prompt with specialist artifacts and DOT content
            specialist_artifacts = {
                role: output["artifact"] for role, output in successful.items()
            }
            
            dot_content = load_dot_content(kb_dir)
            if not dot_content:
                dot_content = dot_overview or f"Topic: {topic}"
            
            curator_prompt = self._build_curator_prompt(
                topic=topic,
                specialist_artifacts=specialist_artifacts,
                dot_content=dot_content,
            )
            
            # Find best available leg: anthropic > openai > gemini
            curator_leg = self._select_curator_leg()
            if not curator_leg:
                error_msg = "No available legs for curator call"
                logger.error(error_msg)
                return self._stub_persona(topic, module_id, error_msg)
            
            # Fire curator LLM call
            curator_response = await self._call_curator(curator_leg, curator_prompt)
            if not curator_response:
                error_msg = "Curator LLM call failed or returned empty"
                logger.error(error_msg)
                return self._stub_persona(topic, module_id, error_msg)
            
            # Parse curator response as JSON
            persona = self.engine._extract_json_artifact(curator_response)
            if not isinstance(persona, dict):
                error_msg = "Curator response was not valid JSON dict"
                logger.error(error_msg)
                return self._stub_persona(topic, module_id, error_msg)
            
            # Stamp metadata
            persona = self._stamp_metadata(persona, topic, successful, module_id)
            
            # Validate routing rules against disk
            persona = self._validate_routing(persona, kb_dir)
            
            logger.info(f"Persona synthesized for topic {topic}")
            return persona
            
        except Exception as e:
            error_msg = f"Unexpected error in synthesize: {type(e).__name__}: {str(e)}"
            logger.exception(error_msg)
            return self._stub_persona(topic, module_id, error_msg)
    
    def _build_curator_prompt(
        self,
        topic: str,
        specialist_artifacts: dict[str, dict],
        dot_content: str,
    ) -> str:
        """Build the user prompt for curator LLM call."""
        specialist_json = json.dumps(specialist_artifacts, indent=2)
        
        return f"""You are synthesizing a GM Persona for the topic: {topic}

Domain of Truth (knowledge base):
{dot_content}

Specialist outputs (narrative, mechanics, philosophy):
{specialist_json}

Synthesize these into a single cohesive GM Persona JSON document.
Apply conflict resolution weights as specified in your system instructions.
Output ONLY the JSON object."""
    
    def _select_curator_leg(self):
        """
        Select best available leg for curator call.
        Prefers anthropic > openai > gemini, first alive leg wins.
        """
        preference_order = ["anthropic", "openai", "gemini"]
        registry = self.engine.registry
        
        for leg_name in preference_order:
            leg = registry.legs.get(leg_name)
            if leg and leg.alive:
                logger.debug(f"Selected leg for curator: {leg_name}")
                return leg
        
        # Fallback to any available leg
        for leg_name, leg in registry.legs.items():
            if leg.alive:
                logger.debug(f"Fallback leg for curator: {leg_name}")
                return leg
        
        return None
    
    async def _call_curator(self, leg, prompt: str) -> Optional[str]:
        """
        Fire curator LLM call on the given leg.
        
        Args:
            leg: Leg instance with provider
            prompt: User prompt for curator
        
        Returns:
            LLM response text, or None on failure
        """
        try:
            # Build messages for leg.provider.complete()
            messages = [{"role": "user", "content": prompt}]
            
            # Infer model name from leg; fallback to generic model name
            model_name = getattr(leg, "model_name", "default-model")
            
            # Call provider synchronously in thread pool to avoid blocking
            response = await asyncio.to_thread(
                leg.provider.complete,
                model_name,
                CURATOR_SYSTEM,
                messages,
            )
            return response if response else None
        except Exception as e:
            logger.error(f"Curator LLM call failed: {e}")
            return None
    
    def _stamp_metadata(
        self,
        persona: dict,
        topic: str,
        successful: dict,
        module_id: Optional[str],
    ) -> dict:
        """
        Stamp metadata onto persona dict.
        Ensures meta section exists with required fields.
        """
        if "meta" not in persona:
            persona["meta"] = {}
        
        meta = persona["meta"]
        
        # Preserve curator-generated meta but fill in guaranteed fields
        meta.setdefault("personaId", f"gm-{topic.replace('-', '_')}")
        meta.setdefault("moduleId", module_id)
        meta.setdefault("personaVersion", "1.0.0")
        meta.setdefault("createdAt", datetime.utcnow().isoformat() + "Z")
        meta.setdefault("specialists", list(successful.keys()))
        meta.setdefault("dotTopic", topic)
        meta["status"] = "active"
        
        return persona
    
    def _validate_routing(self, persona: dict, kb_dir: Path) -> dict:
        """
        Validate that dotPath entries in routingRules exist on disk.
        
        Checks each rule's dotPath after stripping #fragment.
        Missing paths are flagged in curatorNotes with VALIDATION WARNINGS block.
        
        Args:
            persona: Persona dict with routingRules
            kb_dir: Root knowledge base directory
        
        Returns:
            Updated persona dict with validation warnings in curatorNotes
        """
        routing_rules = persona.get("routingRules", [])
        missing_paths = []
        
        for rule in routing_rules:
            dot_path = rule.get("dotPath")
            if not dot_path:
                continue
            
            # Strip fragment (e.g., "path/to/file.md#section" -> "path/to/file.md")
            if "#" in dot_path:
                dot_path = dot_path.split("#")[0]
            
            # Check if file exists relative to kb_dir
            full_path = kb_dir / dot_path
            if not full_path.exists():
                rule["_warning"] = f"dotPath not found: {dot_path}"
                missing_paths.append(dot_path)
                logger.warning(f"Routing rule dotPath missing: {dot_path}")
        
        # Append validation warnings to curatorNotes
        if missing_paths:
            notes = persona.get("curatorNotes", "")
            validation_block = "\n\n[VALIDATION WARNINGS]\n"
            validation_block += f"Missing DOT paths ({len(missing_paths)}):\n"
            for path in missing_paths:
                validation_block += f"  - {path}\n"
            validation_block += "These rules may fail at runtime if paths are not created."
            
            persona["curatorNotes"] = notes + validation_block if notes else validation_block
        
        return persona
    
    @staticmethod
    def _stub_persona(topic: str, module_id: Optional[str], error: str) -> dict:
        """
        Return minimal stub persona on synthesis failure.
        
        Includes meta.status='stub' and meta.error for debugging.
        Contains only required structure; no routing rules or faction voices.
        
        Args:
            topic: Knowledge base topic
            module_id: Optional module identifier
            error: Error message explaining failure
        
        Returns:
            Minimal persona dict with failure indicators
        """
        return {
            "meta": {
                "personaId": f"gm-stub-{topic.replace('-', '_')}",
                "moduleId": module_id,
                "personaVersion": "1.0.0",
                "createdAt": datetime.utcnow().isoformat() + "Z",
                "specialists": [],
                "dotTopic": topic,
                "status": "stub",
                "error": error,
            },
            "coreInstructions": {
                "identity": f"GM for {topic}",
                "authority": "DOT knowledge base",
                "failureMode": "Admit uncertainty and ask the player to rephrase.",
            },
            "voiceFramework": {},
            "routingRules": [],
            "factionVoices": [],
            "fallbackProtocol": {
                "routingFailure": "I'm not certain about that. Could you rephrase?",
                "dotMissing": "The records on that are unclear.",
                "lowConfidence": "Let me check — what exactly would you like to know?",
            },
            "curatorNotes": f"Synthesis failed: {error}. Run persona build again.",
        }
