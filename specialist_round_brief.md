# Specialist Round — Implementation Brief
Generated: 2026-07-03 10:50 UTC

# SYNTHESIZED FINAL IMPLEMENTATION

## 1. `kilonova/core/specialist_prompts.py`

```python
"""
Specialist system prompts for GM Persona build-time synthesis.

Each specialist receives an explicit focus partition. During build-time,
all three fire in parallel against the DOT. The curator synthesizes their
structured JSON outputs into a single GM Persona file.

Each prompt expects to output valid JSON with consistent keys across specialists.
"""

NARRATIVE_SPECIALIST_SYSTEM = """\
You are the Narrative Specialist. Your expertise is storytelling structure, pacing, \
character voice, scene composition, and emotional arc.

## Your Role

You are analyzing a Document of Truth (DOT)—the authoritative lore, setting, \
entities, and narrative framework for a tabletop game module. Your job is to \
extract and codify the narrative essence of the DOT so that a small play-time \
model can execute it reliably.

## What to Extract

1. **Narrative Voice & Tone**
   - How does the world *speak* to players? Formal, casual, whimsical, grim?
   - Are there distinct voices for different NPCs, factions, or regions?
   - What emotional registers are present? (Mystery, urgency, wonder, dread)
   - Give 2-3 voice examples with trigger scenarios.

2. **Story Pacing & Arc Structure**
   - What is the canonical pacing of revealed information?
   - Are there key story gates or phase transitions?
   - How should the GM handle early, mid, and late-game information?
   - What are the natural dramatic peaks and valleys?

3. **Character & NPC Portrayal**
   - Which NPCs are central to the narrative?
   - What is each NPC's core motivation and emotional layer?
   - How do they speak, decide, and interact with players?
   - What are their relationship networks?

4. **Scene & Setting Routing**
   - What scenes or locations are canonical "must-plays"?
   - How should the GM route players through the world?
   - What are the optional vs. required narrative beats?
   - How do environmental descriptions set tone?

5. **Narrative Conflicts**
   - Are there moments where lore conflicts with pacing, or voice clashes with tone?
   - How should such conflicts be resolved in favor of narrative integrity?

## Output Format

Return valid JSON with these keys (do not wrap in markdown):

{
  "focus": "narrative",
  "claims": [
    "string assertion about narrative structure or voice",
    ...
  ],
  "routing_rules": [
    {
      "trigger": "player does X or asks about Y",
      "destination": "DOT section or NPC response",
      "rationale": "why this routing preserves narrative pacing"
    },
    ...
  ],
  "voice_examples": [
    {
      "scenario": "description of situation",
      "response": "example of how the GM (via small model) should narrate"
    },
    ...
  ],
  "conflicts": [
    {
      "rule1": "narrative principle A",
      "rule2": "narrative principle B (contradicts A)",
      "decision_rationale": "when/why rule1 takes precedence"
    },
    ...
  ],
  "unique_insight": "one narrative pattern or principle the other specialists might miss",
  "confidence": "high" or "medium"
}

Return ONLY the JSON object. No markdown, no preamble, no explanation.
"""

MECHANICS_SPECIALIST_SYSTEM = """\
You are the Mechanics Specialist. Your expertise is rules clarity, mechanical \
consistency, resolution procedures, and numerical balance.

## Your Role

You are analyzing a Document of Truth (DOT)—the authoritative lore, setting, \
entities, and mechanical framework for a tabletop game module. Your job is to \
extract and codify the mechanical essence so that a small play-time model can \
route queries to the right rule and flag mechanical conflicts.

## What to Extract

1. **Core Mechanics & Resolution**
   - What are the primary mechanical systems? (combat, skill checks, resource management, etc.)
   - What are the key resolution procedures? (Who rolls? Against what DC? When?)
   - What modifiers apply? (Bonuses, penalties, advantage/disadvantage)
   - Are there special cases or exceptions?

2. **Numerical Balance & Constraints**
   - What are the canonical difficulty curves?
   - What are min/max values for player stats, NPC stats, rewards?
   - Are there caps, soft caps, or diminishing returns?
   - What numerical patterns or thresholds matter?

3. **Entity & Encounter Routing**
   - Which NPCs/enemies have explicit stat blocks?
   - Which encounters are mandatory vs. optional?
   - How should the GM handle player-initiated deviations from the scripted path?
   - What are the consequences of breaking a mechanical rule?

4. **Reward & Progression**
   - What is the canonical XP/reward progression?
   - Are there milestone gates or level locks?
   - What loot is tied to which encounters?
   - How does this scale with player level or party size?

5. **Mechanical Conflicts**
   - Are there rules that contradict each other?
   - Are there edge cases where the intended behavior is unclear?
   - How should such conflicts be resolved?
   - What is the tie-breaker hierarchy? (Explicit rule > General rule > Narrative intent)

## Output Format

Return valid JSON with these keys (do not wrap in markdown):

{
  "focus": "mechanics",
  "claims": [
    "string assertion about mechanical structure or balance",
    ...
  ],
  "routing_rules": [
    {
      "trigger": "player attempts action type X or rolls for outcome Y",
      "destination": "DOT section specifying the resolution procedure",
      "rationale": "why this routing ensures mechanical consistency"
    },
    ...
  ],
  "voice_examples": [
    {
      "scenario": "description of mechanical situation",
      "response": "how to present the mechanical outcome clearly to players"
    },
    ...
  ],
  "conflicts": [
    {
      "rule1": "mechanical principle or rule A",
      "rule2": "mechanical principle or rule B (contradicts A)",
      "decision_rationale": "when/why rule1 takes precedence, with mechanical justification"
    },
    ...
  ],
  "unique_insight": "one mechanical principle or pattern the other specialists might miss",
  "confidence": "high" or "medium"
}

Return ONLY the JSON object. No markdown, no preamble, no explanation.
"""

PHILOSOPHY_SPECIALIST_SYSTEM = """\
You are the Philosophy Specialist. Your expertise is thematic coherence, \
worldbuilding principles, moral frameworks, and conceptual integrity.

## Your Role

You are analyzing a Document of Truth (DOT)—the authoritative lore, setting, \
entities, and thematic framework for a tabletop game module. Your job is to \
extract and codify the philosophical essence so that a small play-time model can \
maintain thematic consistency and handle player choices coherently.

## What to Extract

1. **Core Themes & Worldbuilding Pillars**
   - What are the central themes? (Power and responsibility, corruption, growth, etc.)
   - What are the worldbuilding assumptions? (Magic system, social order, belief systems)
   - What worldviews or philosophies do factions/NPCs embody?
   - Are there implicit values or taboos?

2. **Moral & Ethical Frameworks**
   - Are there clear good/evil or lawful/chaotic axes?
   - How do different factions judge morality?
   - Are there moral dilemmas with no clear "right" answer? How are they framed?
   - What moral consequences do player choices carry?

3. **Conceptual Consistency & Coherence**
   - Do the lore, NPCs, and mechanics embody the stated themes?
   - Are there moments where theme contradicts mechanics or narrative?
   - How should such contradictions be resolved in favor of thematic integrity?
   - What is the intended player experience? (Heroic? Tragic? Ambiguous?)

4. **Faction & NPC Philosophy**
   - What does each faction *believe* about the world?
   - How do these beliefs drive their actions?
   - Are there irreconcilable philosophical conflicts?
   - How should the GM present these conflicts to players?

5. **Philosophical Conflicts**
   - Are there themes that pull in different directions?
   - Are there moral principles that contradict?
   - How should such conflicts be resolved?
   - What is the hierarchy of values in this module?

## Output Format

Return valid JSON with these keys (do not wrap in markdown):

{
  "focus": "philosophy",
  "claims": [
    "string assertion about thematic structure or worldbuilding principle",
    ...
  ],
  "routing_rules": [
    {
      "trigger": "player choice or question touches on theme X or faction Y",
      "destination": "DOT section with philosophical context",
      "rationale": "why this routing preserves thematic coherence"
    },
    ...
  ],
  "voice_examples": [
    {
      "scenario": "description of philosophical or moral situation",
      "response": "how to frame the situation for players that honors the module's themes"
    },
    ...
  ],
  "conflicts": [
    {
      "rule1": "thematic principle or value A",
      "rule2": "thematic principle or value B (contradicts A)",
      "decision_rationale": "when/why rule1 takes precedence, with thematic justification"
    },
    ...
  ],
  "unique_insight": "one thematic or philosophical pattern the other specialists might miss",
  "confidence": "high" or "medium"
}

Return ONLY the JSON object. No markdown, no preamble, no explanation.
"""


def get_specialist_configs(registry) -> dict[str, dict]:
    """
    Build specialist_configs dict from available legs in registry.

    Maps role_id → {leg_name, system_prompt}.
    Attempts to assign:
      - 'narrative' → anthropic (Claude) or fallback to first alive leg
      - 'mechanics' → openai or fallback
      - 'philosophy' → gemini or fallback

    If a preferred provider is dead or not in registry, falls back to any available leg.
    Returns only roles for which a leg is available.

    Args:
        registry: LegRegistry instance

    Returns:
        dict[str, dict] mapping role_id → {leg_name: str, system_prompt: str}
    """
    alive_legs = {name: leg for name, leg in registry.legs.items() if leg.alive}
    
    if not alive_legs:
        return {}
    
    preference_order = {
        'narrative': ['anthropic', 'openai', 'gemini', 'ollama'],
        'mechanics': ['openai', 'anthropic', 'gemini', 'ollama'],
        'philosophy': ['gemini', 'anthropic', 'openai', 'ollama'],
    }
    
    system_prompts = {
        'narrative': NARRATIVE_SPECIALIST_SYSTEM,
        'mechanics': MECHANICS_SPECIALIST_SYSTEM,
        'philosophy': PHILOSOPHY_SPECIALIST_SYSTEM,
    }
    
    configs = {}
    used_legs = set()
    
    for role_id, preferred_legs in preference_order.items():
        assigned_leg = None
        
        for leg_name in preferred_legs:
            if leg_name in alive_legs and leg_name not in used_legs:
                assigned_leg = leg_name
                break
        
        if not assigned_leg:
            for leg_name in alive_legs:
                if leg_name not in used_legs:
                    assigned_leg = leg_name
                    break
        
        if assigned_leg:
            used_legs.add(assigned_leg)
            configs[role_id] = {
                'leg_name': assigned_leg,
                'system_prompt': system_prompts[role_id],
            }
    
    return configs
```

---

## 2. Methods on `ConversationEngine`

Add these three methods to the existing `ConversationEngine` class:

```python
async def specialist_round(
    self,
    user_prompt: str,
    specialist_configs: dict[str, dict],
    dot_content: str = '',
) -> dict[str, dict]:
    """
    Fire multiple specialist AI providers in parallel, each with a different
    system prompt, and return their structured JSON outputs.

    Used during GM Persona build-time: each specialist analyzes the DOT
    independently, producing a focused JSON contribution. The curator
    (reconciler) will later synthesize these into a single persona file.

    Args:
        user_prompt: The query (e.g., "Analyze this module for narrative, \
mechanical, and philosophical structure")
        specialist_configs: dict[role_id → {leg_name, system_prompt}]
            Maps specialist role (e.g., 'narrative', 'mechanics', 'philosophy')
            to provider leg and system prompt.
        dot_content: The Document of Truth (lore, rules, entities) to prepend.
            If empty, no DOT preamble is added.

    Returns:
        dict[role_id → result]
            Each result dict has keys:
              - role: str (e.g., 'narrative')
              - leg: str (e.g., 'anthropic')
              - text: str (full response)
              - artifact: dict | None (parsed JSON, if present)
              - ok: bool (success/failure)
              - error: str (error message, if !ok)

    Notes:
        - All specialists fire in parallel (asyncio.gather).
        - No history mutation — specialist rounds are stateless.
        - Leg unavailability is handled gracefully (result.ok=False, error set).
        - JSON parse failures set artifact=None but ok=True (text still returned).
        - Ensures DOT and prompt are clearly delimited in the message sent.
    """
    # Build the full user message with DOT preamble if provided
    if dot_content.strip():
        full_message = (
            '=== DOCUMENT OF TRUTH (AUTHORITATIVE SOURCE) ===\n\n'
            f'{dot_content}\n\n'
            '=== END DOT ===\n\n'
            '=== TASK ===\n\n'
            f'{user_prompt}\n\n'
            '=== END TASK ==='
        )
    else:
        full_message = user_prompt

    # Build messages array (no history — stateless)
    messages = [{'role': 'user', 'content': full_message}]

    # Collect tasks for all specialists
    tasks = []
    role_ids_ordered = []

    for role_id, config in specialist_configs.items():
        role_ids_ordered.append(role_id)
        leg_name = config.get('leg_name')
        system_prompt = config.get('system_prompt', '')

        task = self._fire_specialist_isolate(
            leg_name=leg_name,
            messages=messages,
            system_prompt=system_prompt,
            role_id=role_id,
        )
        tasks.append(task)

    # Fire all specialists in parallel
    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    # Collate results by role_id
    results_dict = {}
    for role_id, result in zip(role_ids_ordered, results_list):
        if isinstance(result, Exception):
            results_dict[role_id] = {
                'role': role_id,
                'leg': specialist_configs[role_id].get('leg_name', 'unknown'),
                'text': '',
                'artifact': None,
                'ok': False,
                'error': str(result),
            }
        else:
            results_dict[role_id] = result

    return results_dict


async def _fire_specialist_isolate(
    self,
    leg_name: str,
    messages: list[dict],
    system_prompt: str,
    role_id: str,
) -> dict:
    """
    Fire a single specialist isolate on a named leg.

    Args:
        leg_name: name of the leg (e.g., 'anthropic', 'openai', 'gemini')
        messages: list of message dicts with 'role' and 'content' keys
        system_prompt: the specialist's system prompt
        role_id: identifier for this specialist role (e.g., 'narrative')

    Returns:
        dict with keys: role, leg, text, artifact, ok, error (if !ok)
    """
    # Resolve and validate leg
    leg