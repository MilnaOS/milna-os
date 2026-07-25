/// A DOT (Document of Truth) card — a single persistent, structured
/// knowledge artifact injected into a model call as authoritative context.
///
/// Mirrors Soutuk's real `DotCard` DTO (`lib/domain/dtos/dot_payload_dto.dart`)
/// deliberately, field-for-field, so an app's real DOT source can implement
/// [DotPayloadSource] against its existing data without a translation
/// layer in between. Kilonova's Python engine has the same underlying
/// concept and name (`engine.py`'s `specialist_round()`, wrapping content
/// in `=== DOCUMENT OF TRUTH ===` delimiters) — this reuses that mechanism,
/// not a new invention.
class DotCard {
  final String cardId;
  final String languageIso;
  final String content;

  const DotCard({
    required this.cardId,
    required this.languageIso,
    required this.content,
  });

  /// Wraps [content] with the same explicit delimiters Kilonova's Python
  /// engine already uses, so a model sees identically-shaped grounding
  /// regardless of which orchestrator implementation is calling it.
  String wrap(String taskPrompt) {
    return '=== DOCUMENT OF TRUTH (AUTHORITATIVE SOURCE) ===\n\n'
        '$content\n\n'
        '=== END DOT ===\n\n'
        '=== TASK ===\n\n'
        '$taskPrompt\n\n'
        '=== END TASK ===';
  }
}

/// The full DOT payload for one translation request — source-language
/// grammar, target-language grammar, and an optional hazard card
/// (medical/legal domain warnings). Mirrors Soutuk's real `DotPayloadDto`
/// shape exactly, including the fact that [hazardCard] is nullable —
/// most requests don't trigger a hazard, and the real implementation only
/// attaches one when medical/legal terms are actually detected.
class DotPayload {
  final String sourceLanguageIso;
  final String targetLanguageIso;
  final DotCard sourceCard;
  final DotCard targetCard;
  final DotCard? hazardCard;

  const DotPayload({
    required this.sourceLanguageIso,
    required this.targetLanguageIso,
    required this.sourceCard,
    required this.targetCard,
    this.hazardCard,
  });

  /// Concatenates the cards that actually apply into one grounding block —
  /// target-language grammar first (what the translators most need), then
  /// source, then the hazard card last if present, so it reads as the most
  /// urgent/recent thing in context rather than getting buried first.
  String get combinedContent {
    final parts = [targetCard.content, sourceCard.content];
    if (hazardCard != null) parts.add(hazardCard!.content);
    return parts.join('\n\n---\n\n');
  }
}

/// Loads a DOT payload for a translation request. The real implementation
/// is app-specific — Soutuk's version wraps its existing
/// `DotIndexQueryManager.sliceActivePayload()` (vocabulary token-matching,
/// hazard-term detection, custom-chunk merging all stay there; that's
/// app-specific business logic, not something this generic engine package
/// should own or duplicate). This package only needs the *shape*, not the
/// slicing behavior behind it.
abstract class DotPayloadSource {
  Future<DotPayload> loadActivePayload(
    String sourceIso,
    String targetIso, [
    String queryText = '',
  ]);
}
