"""
scrape_corpus.py — Milna scrape mode entry point.

Crawls a rules SRD site, cleans each page with a cheap parser leg,
and appends numbered chunks to a flat corpus file.  The corpus is raw
stockpile — build_gm_persona.py distills it into a DOT later.

Usage:
    python scrape_corpus.py --topic starfinder
    python scrape_corpus.py --topic starfinder --max-pages 200 --no-discover

The corpus lands at:
    kb/<topic>/corpus.txt

Seed URL lists are defined in SEED_URLS below.  Add more topics as needed.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.legs        import LegRegistry, KeyInventory, Leg
from kilonova.core.mode        import ModeController
from kilonova.core.providers.anthropic_provider import AnthropicProvider
from kilonova.core.providers.ollama_provider import OllamaProvider
from kilonova.core.engine      import ConversationEngine
from kilonova.tools.corpus_collector import CorpusCollector


# ── Seed URLs per topic ───────────────────────────────────────────────────────
# aonsrd.com = Archives of Nethys — official Starfinder SRD

SEED_URLS: dict[str, list[str]] = {
    'starfinder': [
        'https://www.aonsrd.com/Rules.aspx?System=0',
        'https://www.aonsrd.com/Classes.aspx',
        'https://www.aonsrd.com/Skills.aspx',
        'https://www.aonsrd.com/Feats.aspx',
        'https://www.aonsrd.com/Spells.aspx',
        'https://www.aonsrd.com/Equipment.aspx',
        'https://www.aonsrd.com/Races.aspx',
        'https://www.aonsrd.com/Aliens.aspx',
        'https://www.aonsrd.com/Starships.aspx',
        'https://www.aonsrd.com/Conditions.aspx',
    ],
    'pathfinder': [
        'https://www.aonprd.com/Rules.aspx',
        'https://www.aonprd.com/Classes.aspx',
        'https://www.aonprd.com/Skills.aspx',
        'https://www.aonprd.com/Feats.aspx',
    ],
    # Sarna.net — BattleTech wiki. Seeded at the IS BattleMech category so the
    # crawler discovers individual mech pages alphabetically. Weapons, equipment,
    # and faction pages follow from link discovery. Use --delay 2.0 to stay polite.
    'linguistics': [
        # World Atlas of Language Structures — cross-linguistic structural properties
        'https://wals.info/chapter',
        'https://wals.info/feature',
        # Wikipedia — language families, universals, typology
        'https://en.wikipedia.org/wiki/List_of_language_families',
        'https://en.wikipedia.org/wiki/Linguistic_universal',
        'https://en.wikipedia.org/wiki/Language_typology',
        'https://en.wikipedia.org/wiki/Morphological_typology',
        'https://en.wikipedia.org/wiki/Phonological_typology',
        'https://en.wikipedia.org/wiki/Syntactic_typology',
        'https://en.wikipedia.org/wiki/Word_order',
        'https://en.wikipedia.org/wiki/Morpheme',
        'https://en.wikipedia.org/wiki/Phoneme',
        'https://en.wikipedia.org/wiki/Grammar',
        # Language family anchors — syntax + phonology overviews
        'https://en.wikipedia.org/wiki/Indo-European_languages',
        'https://en.wikipedia.org/wiki/Sino-Tibetan_languages',
        'https://en.wikipedia.org/wiki/Afroasiatic_languages',
        'https://en.wikipedia.org/wiki/Niger-Congo_languages',
        'https://en.wikipedia.org/wiki/Austronesian_languages',
        'https://en.wikipedia.org/wiki/Dravidian_languages',
        'https://en.wikipedia.org/wiki/Turkic_languages',
        'https://en.wikipedia.org/wiki/Japonic_languages',
        'https://en.wikipedia.org/wiki/Koreanic_languages',
        'https://en.wikipedia.org/wiki/Semitic_languages',
    ],
    'battletech': [
        'https://www.sarna.net/wiki/Category:Inner_Sphere_BattleMechs',
        'https://www.sarna.net/wiki/Category:BattleMechs_Introduced_in_the_Succession_Wars',
        'https://www.sarna.net/wiki/Category:Weapons',
        'https://www.sarna.net/wiki/Category:Equipment',
        # Anchor pages — ensure core mechs are hit even if category links drift
        'https://www.sarna.net/wiki/Archer',
        'https://www.sarna.net/wiki/Warhammer',
        'https://www.sarna.net/wiki/Marauder',
        'https://www.sarna.net/wiki/Atlas',
        'https://www.sarna.net/wiki/Hunchback',
        'https://www.sarna.net/wiki/Jenner',
        'https://www.sarna.net/wiki/Zeus',
        'https://www.sarna.net/wiki/Vindicator',
        'https://www.sarna.net/wiki/Enforcer',
        'https://www.sarna.net/wiki/Dragon',
        'https://www.sarna.net/wiki/Catapult',
        'https://www.sarna.net/wiki/BattleMaster',
        'https://www.sarna.net/wiki/LRM-20',
        'https://www.sarna.net/wiki/PPC',
        'https://www.sarna.net/wiki/Autocannon/10',
    ],

    # AToW character-creation data — skills, traits, life paths.
    # Sarna has overview and some detail pages; the LLM filters non-AToW hits.
    'battletech_atow': [
        'https://www.sarna.net/wiki/A_Time_of_War',
        'https://www.sarna.net/wiki/Category:A_Time_of_War',
        'https://www.sarna.net/wiki/Skills_(A_Time_of_War)',
        'https://www.sarna.net/wiki/Traits_(A_Time_of_War)',
        'https://www.sarna.net/wiki/Life_Paths',
        'https://www.sarna.net/wiki/Character_Creation_(A_Time_of_War)',
        'https://www.sarna.net/wiki/Piloting',
        'https://www.sarna.net/wiki/Gunnery',
        'https://www.sarna.net/wiki/Athletics',
        'https://www.sarna.net/wiki/Perception',
        'https://www.sarna.net/wiki/Technician',
        'https://www.sarna.net/wiki/MechWarrior_(pilot)',
        'https://www.sarna.net/wiki/Clan_Warrior',
        'https://www.sarna.net/wiki/Inner_Sphere',
    ],

    # Equipment and weapons crawl — focused on sarna equipment/weapon categories.
    # Uses _is_battletech_equip_url filter to stay on-topic.
    'battletech_equip': [
        'https://www.sarna.net/wiki/Category:Weapons',
        'https://www.sarna.net/wiki/Category:Equipment',
        'https://www.sarna.net/wiki/Category:Ballistic_Weapons',
        'https://www.sarna.net/wiki/Category:Energy_Weapons',
        'https://www.sarna.net/wiki/Category:Missile_Weapons',
        'https://www.sarna.net/wiki/Category:Physical_Weapons',
        'https://www.sarna.net/wiki/Category:Electronic_Warfare',
        'https://www.sarna.net/wiki/Category:Ammunition',
        # Anchor articles for core weapons
        'https://www.sarna.net/wiki/Autocannon',
        'https://www.sarna.net/wiki/PPC',
        'https://www.sarna.net/wiki/Large_Laser',
        'https://www.sarna.net/wiki/Medium_Laser',
        'https://www.sarna.net/wiki/Small_Laser',
        'https://www.sarna.net/wiki/LRM-20',
        'https://www.sarna.net/wiki/SRM-6',
        'https://www.sarna.net/wiki/Gauss_Rifle',
        'https://www.sarna.net/wiki/Flamer',
        'https://www.sarna.net/wiki/Machine_Gun',
    ],

    # Targeted mech-only crawl — follows sarna's own category structure.
    # Stays inside BattleMech and weight-class categories; skips prototypes,
    # lore articles, pilots, manufacturers, and anything not a mech stat page.
    # Use --delay 2.0 to stay polite. No max-pages needed — filter keeps scope tight.
    'battletech_mechs': [
        # Era categories
        'https://www.sarna.net/wiki/Category:BattleMechs_Introduced_in_the_Age_of_War',
        'https://www.sarna.net/wiki/Category:BattleMechs_Introduced_in_the_Star_League',
        'https://www.sarna.net/wiki/Category:BattleMechs_Introduced_in_the_Succession_Wars',
        'https://www.sarna.net/wiki/Category:BattleMechs_Introduced_in_the_Clan_Invasion',
        'https://www.sarna.net/wiki/Category:BattleMechs_Introduced_in_the_Civil_War',
        # Weight class categories
        'https://www.sarna.net/wiki/Category:Light_BattleMechs',
        'https://www.sarna.net/wiki/Category:Medium_BattleMechs',
        'https://www.sarna.net/wiki/Category:Heavy_BattleMechs',
        'https://www.sarna.net/wiki/Category:Assault_BattleMechs',
        # Clan OmniMech categories
        'https://www.sarna.net/wiki/Category:OmniMechs',
        'https://www.sarna.net/wiki/Category:Clan_BattleMechs',
        # Inner Sphere root
        'https://www.sarna.net/wiki/Category:Inner_Sphere_BattleMechs',
    ],
}


# ── URL filters ──────────────────────────────────────────────────────────────

def _is_battletech_mech_url(url: str) -> bool:
    """
    True if this sarna.net URL belongs in a mech-only crawl.

    Allowed:
      - Weight-class or era Category pages for BattleMechs / OmniMechs
      - Regular /wiki/ pages (individual mech articles)

    Blocked:
      - MediaWiki namespace pages (User:, Talk:, File:, Template:, Help:, Special:)
      - Prototype pages (excluded by design — page-level extraction rejects them anyway,
        but blocking here saves the LLM call entirely)
      - Category pages that aren't mech-related (Vehicles, Weapons, Pilots, etc.)
    """
    from urllib.parse import urlparse as _up
    path = _up(url).path

    # Reject known non-content namespaces
    for ns in ('User:', 'Talk:', 'File:', 'Template:', 'Help:', 'Special:'):
        if ns in path:
            return False

    # Reject prototype pages
    if 'Prototype' in path or 'prototype' in path:
        return False

    # Category pages — only follow mech-class categories
    if 'Category:' in path:
        mech_keywords = ('BattleMech', 'OmniMech', 'Mech', 'Light_Battle', 'Medium_Battle',
                         'Heavy_Battle', 'Assault_Battle', 'Age_of_War', 'Star_League',
                         'Succession_Wars')
        return any(kw in path for kw in mech_keywords)

    # Regular wiki article — allow (ingest will return null for non-mech pages, cost is low)
    return '/wiki/' in path


def _is_battletech_equip_url(url: str) -> bool:
    """
    True if this sarna.net URL belongs in an equipment/weapons crawl.
    Follows equipment and weapon category pages; rejects non-content namespaces
    and unrelated categories (mechs, pilots, lore articles, etc.).
    """
    from urllib.parse import urlparse as _up
    path = _up(url).path
    for ns in ('User:', 'Talk:', 'File:', 'Template:', 'Help:', 'Special:'):
        if ns in path:
            return False
    if 'Category:' in path:
        equip_keywords = (
            'Weapon', 'Equipment', 'Ammunition', 'Ballistic', 'Energy',
            'Missile', 'Physical_Weapon', 'Electronic', 'Support_Weapon',
            'Artillery', 'Anti_Personnel',
        )
        return any(kw in path for kw in equip_keywords)
    return '/wiki/' in path


# ── Topic → URL filter map ────────────────────────────────────────────────────

_URL_FILTERS: dict[str, 'callable'] = {
    'battletech_mechs':  _is_battletech_mech_url,
    'battletech_equip':  _is_battletech_equip_url,
}


# ── Engine setup ──────────────────────────────────────────────────────────────

def _build_engine() -> tuple[ConversationEngine, LegRegistry]:
    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()

    active = registry.active_legs()
    if not active:
        print('ERROR: No API keys found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY.')
        sys.exit(1)

    mode = ModeController('potato')  # scrape mode = minimal — one parser call per page

    # Reconciler uses cheapest available leg
    leg = active[0]
    engine = ConversationEngine(
        leg_registry=registry,
        mode_controller=mode,
        reconciler_provider=leg.provider,
        reconciler_model=leg.model,
    )
    return engine, registry


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    topic     = args.topic.lower()
    seeds     = SEED_URLS.get(topic)

    if not seeds:
        print(f'ERROR: No seed URLs defined for topic "{topic}".')
        print(f'Available topics: {list(SEED_URLS.keys())}')
        print('Add seeds to SEED_URLS in this file, then re-run.')
        sys.exit(1)

    if args.url:
        seeds = [args.url] + seeds

    corpus_path = Path('kb') / topic / 'corpus.txt'

    print(f'Corpus Collection — {topic}')
    print('=' * 50)
    print(f'  Seeds   : {len(seeds)} URLs')
    print(f'  Max pages: {args.max_pages}')
    print(f'  Discover : {not args.no_discover}')
    print(f'  Corpus  : {corpus_path}')
    print(f'  Delay   : {args.delay}s between requests')
    print()

    engine, registry = _build_engine()

    parser_legs = _build_parser_legs(registry, free_only=args.free_only)
    concurrency = len(parser_legs)
    print(f'  Parser legs ({concurrency} workers):')
    for i, name in enumerate(parser_legs):
        print(f'    worker-{i}: {name}')
    print()

    url_filter = _URL_FILTERS.get(topic)
    if url_filter:
        print(f'  URL filter : {url_filter.__name__}')
        print()

    collector = CorpusCollector(
        engine=engine,
        corpus_path=corpus_path,
        parser_leg=parser_legs,
        concurrency=concurrency,
        delay_between_requests=args.delay,
        url_filter=url_filter,
    )

    await collector.collect(
        seed_urls=seeds,
        max_pages=args.max_pages,
        discover_links=not args.no_discover,
        shuffle=args.shuffle,
        verbose=True,
    )


def _build_parser_legs(registry: LegRegistry, max_ollama: int = 3, free_only: bool = False) -> list[str]:
    """
    Build parser leg list. free_only=True excludes Anthropic, OpenAI, and Gemini
    (paid tiers) — uses only Groq, together, cerebras, openrouter, sambanova, Ollama.
    """
    _PAID = {'anthropic', 'openai', 'gemini'}
    legs_out: list[str] = []

    # Ollama — always free
    base_ollama = registry.legs.get('ollama')
    if base_ollama and base_ollama.alive and registry.ollama_models:
        for model in registry.ollama_models[:max_ollama]:
            leg_name = f'ollama:{model}'
            registry.legs[leg_name] = Leg(
                name=leg_name,
                provider=OllamaProvider(base_url=registry.ollama_url, model=model),
                model=model,
                alive=True,
            )
            legs_out.append(leg_name)

    # Free-tier cloud legs
    free_cloud = ['groq', 'together', 'cerebras', 'openrouter', 'sambanova']
    for name in free_cloud:
        if name in registry.legs and registry.legs[name].alive:
            legs_out.append(name)

    # If not free_only, also add paid legs
    if not free_only:
        for name in _PAID:
            if name in registry.legs and registry.legs[name].alive:
                legs_out.append(name)

    # Fallback: anything active
    if not legs_out:
        active = [l for l in registry.active_legs() if not free_only or l.name not in _PAID]
        legs_out = [active[0].name] if active else ['groq']

    return legs_out


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Milna scrape mode — build a raw corpus from an SRD site'
    )
    parser.add_argument(
        '--topic', required=True,
        help='Knowledge base topic (e.g. starfinder, pathfinder)'
    )
    parser.add_argument(
        '--url',
        help='Extra seed URL to add to the default list'
    )
    parser.add_argument(
        '--max-pages', type=int, default=0,
        help='Hard cap on pages to process (default: 0 = unlimited)'
    )
    parser.add_argument(
        '--no-discover', action='store_true',
        help='Do not follow links — only scrape seed URLs'
    )
    parser.add_argument(
        '--delay', type=float, default=1.0,
        help='Seconds to wait between requests per worker (default: 1.0)'
    )
    parser.add_argument(
        '--shuffle', action='store_true',
        help='Randomise URL crawl order — gives representative sample if run ends early'
    )
    parser.add_argument(
        '--free-only', action='store_true',
        help='Use only free-tier legs (Groq, together, cerebras, openrouter, sambanova, Ollama) — skip Anthropic, OpenAI, Gemini'
    )
    return parser.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
