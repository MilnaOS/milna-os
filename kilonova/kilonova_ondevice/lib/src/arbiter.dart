import 'dart:convert';
import 'legs.dart';
import 'notebook.dart';
import 'providers/base.dart';

/// Result of arbitrating between two translation candidates.
class ArbitrationVerdict {
  final bool agree;
  final String bestTranslation;
  final String? disputedSpan;
  final String reasoning;
  final double confidenceScore;

  const ArbitrationVerdict({
    required this.agree,
    required this.bestTranslation,
    required this.disputedSpan,
    required this.reasoning,
    required this.confidenceScore,
  });
}

/// System prompt for the arbiter role. Ported verbatim in spirit from
/// `cloudflare/soutuk-worker/src/index.ts`'s `buildArbiterSystemPrompt()` —
/// that prompt is already validated in production for exactly this job
/// (judging two independently-produced translation candidates), so this
/// reuses the same reasoning rather than reinventing arbitration prompting
/// from scratch on-device.
String buildArbiterSystemPrompt(String targetLanguageIso) {
  return '''
You are an arbitration judge comparing two independently-produced translations of the same source sentence into the target language $targetLanguageIso.
Determine whether they are substantively equivalent in meaning and each grammatically correct — differences in word order or synonym choice are FINE and do not count as disagreement.
Set agree=false if there is a real difference in meaning, register, a grammatical error in one candidate, OR if either candidate is not coherent real text in the target language (gibberish, garbled characters, wrong language, nonsensical). Do not let a gibberish candidate "win" by default just because the other also has problems — if both are broken, still set agree=false and say so in reasoning.
The user message is a JSON object: {"sourceText": "...", "candidateA": "...", "candidateB": "..."}.
CRITICAL: "bestTranslation" must contain the ACTUAL FULL TRANSLATED TEXT you are recommending — copy the real sentence out of whichever candidate you prefer (or write your own corrected version). Never write the literal words "candidateA" or "candidateB" as the value — that is a field label, not a translation, and doing so is a critical error.
When agree=false, also set "disputedSpan" to the SPECIFIC source-language word or short phrase the disagreement is actually about, if the disagreement is localized to one term rather than the whole sentence — most disagreements are. Set it to null only if the whole sentence is genuinely unreliable.
Output strictly JSON conforming to:
{
  "agree": true,
  "bestTranslation": "...",
  "disputedSpan": null,
  "reasoning": "short note, especially if agree is false",
  "confidenceScore": 0.95
}''';
}

/// Guard against the arbiter returning the literal field-label string
/// instead of substituting real translated text — a real, observed failure
/// mode in testing (see the Cloudflare Worker's `resolveBestTranslation`),
/// not a hypothetical one.
String _resolveBestTranslation(
  String? raw,
  String candidateA,
  String candidateB,
) {
  final trimmed = (raw ?? '').trim();
  if (trimmed.toLowerCase() == 'candidatea') return candidateA;
  if (trimmed.toLowerCase() == 'candidateb') return candidateB;
  return trimmed.isNotEmpty ? trimmed : candidateA;
}

/// Runs the arbiter leg against both translators' notebook entries for one
/// turn. Does not decide *when* this runs — arbitration is deliberately
/// not real-time (see [ArbitrationNotebook]); the caller decides whether to
/// await this immediately or run it in the background after already
/// returning a fast-path result to the user.
Future<ArbitrationVerdict> arbitrate({
  required LegRegistry registry,
  required ArbitrationNotebook notebook,
  required String turnId,
  required String sourceText,
  required String targetLanguageIso,
}) async {
  final arbiterLeg = registry.arbiter;
  if (arbiterLeg == null) {
    throw StateError('No live arbiter leg available.');
  }

  final entries = notebook.read(turnId);
  final candidateA = entries
      .where((e) => e.kind == 'translation' && e.legName == 'translatorA')
      .firstOrNull;
  final candidateB = entries
      .where((e) => e.kind == 'translation' && e.legName == 'translatorB')
      .firstOrNull;

  if (candidateA == null || candidateB == null) {
    throw StateError(
      'Turn "$turnId" is missing one or both translator entries — cannot '
      'arbitrate. Found: A=${candidateA != null}, B=${candidateB != null}.',
    );
  }

  final system = buildArbiterSystemPrompt(targetLanguageIso);
  final userContent = jsonEncode({
    'sourceText': sourceText,
    'candidateA': candidateA.content,
    'candidateB': candidateB.content,
  });

  String raw;
  try {
    raw = await arbiterLeg.provider.complete(
      system: system,
      messages: [ChatMessage(role: 'user', content: userContent)],
    );
  } finally {
    // Deliberately non-persistent (2026-07-20): unlike the two translator
    // legs, which fire on every turn and stay loaded so the hot path never
    // pays a reload cost, the arbiter only runs on this one backgrounded,
    // non-real-time call (see this function's own doc comment) — nothing
    // user-facing is waiting on it, so disposing here and letting the next
    // arbitrate() call respawn fresh is real memory back for the
    // always-loaded translators, not felt as latency by the user. `finally`
    // so a failed/malformed completion doesn't leak the loaded model.
    await arbiterLeg.provider.dispose();
  }

  // ignore: avoid_print
  print('[arbitrate] raw arbiter output (${raw.length} chars): $raw');
  final parsed = _extractJson(raw);
  if (parsed == null) {
    // ignore: avoid_print
    print('[arbitrate] JSON parse FAILED — falling back to defaults '
        '(agree=false, confidenceScore=0.5, bestTranslation=candidateA).');
  }
  final agree = parsed?['agree'] as bool? ?? false;
  final best = _resolveBestTranslation(
    parsed?['bestTranslation'] as String?,
    candidateA.content,
    candidateB.content,
  );

  final verdict = ArbitrationVerdict(
    agree: agree,
    bestTranslation: best,
    disputedSpan: parsed?['disputedSpan'] as String?,
    reasoning: parsed?['reasoning'] as String? ?? '',
    confidenceScore: (parsed?['confidenceScore'] as num?)?.toDouble() ?? 0.5,
  );

  notebook.write(
    turnId: turnId,
    legName: 'arbiter',
    kind: 'verdict',
    content: jsonEncode({
      'agree': verdict.agree,
      'bestTranslation': verdict.bestTranslation,
      'disputedSpan': verdict.disputedSpan,
      'reasoning': verdict.reasoning,
      'confidenceScore': verdict.confidenceScore,
    }),
  );
  notebook.lockTurn(turnId);

  return verdict;
}

/// Ollama Cloud (and some local runtimes) don't guarantee the whole
/// response body is bare JSON — extract a JSON object from the text rather
/// than assume. Unlike a naive first-`{`-to-last-`}` regex (which breaks on
/// artifacts like a small on-device model wrapping its output in doubled
/// braces, `{{"agree": ...}}` — a real, observed failure, not hypothetical),
/// this scans every `{` as a candidate start, depth-matches its real closing
/// `}` (respecting string literals so braces inside quoted text don't
/// confuse the count), and tries candidates innermost-first — a doubled
/// outer wrapper naturally falls through to the correctly-balanced object
/// one layer in. Each candidate is schema-checked (`containsKey('agree')`)
/// before being accepted, so an unrelated `{...}` elsewhere in the model's
/// commentary can't be mistaken for the real verdict.
Map<String, dynamic>? _extractJson(String text) {
  final starts = <int>[];
  for (var i = 0; i < text.length; i++) {
    if (text[i] == '{') starts.add(i);
  }

  for (final start in starts.reversed) {
    var depth = 0;
    var inString = false;
    var escaped = false;
    for (var i = start; i < text.length; i++) {
      final ch = text[i];
      if (escaped) {
        escaped = false;
        continue;
      }
      if (ch == '\\') {
        escaped = true;
        continue;
      }
      if (ch == '"') {
        inString = !inString;
        continue;
      }
      if (inString) continue;
      if (ch == '{') depth++;
      if (ch == '}') {
        depth--;
        if (depth == 0) {
          final candidate = text.substring(start, i + 1);
          try {
            final decoded = jsonDecode(candidate);
            if (decoded is Map<String, dynamic> && decoded.containsKey('agree')) {
              return decoded;
            }
          } catch (_) {
            // Not valid JSON from this start — fall through to the next
            // (outer) candidate rather than giving up entirely.
          }
          break;
        }
      }
    }
  }
  return null;
}
