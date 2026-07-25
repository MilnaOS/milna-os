import 'arbiter.dart';
import 'dot.dart';
import 'legs.dart';
import 'notebook.dart';
import 'providers/base.dart';

/// Result of one translation turn.
///
/// [fastResult] is available as soon as the first translator finishes —
/// arbitration is deliberately not real-time (see [ArbitrationNotebook]),
/// so the caller isn't expected to block on [verdict] before showing
/// something to the user. Await [verdict] separately, whenever it's
/// actually convenient to surface a correction.
class TranslationTurn {
  final String turnId;
  final String fastResult;
  final Future<ArbitrationVerdict> verdict;

  const TranslationTurn({
    required this.turnId,
    required this.fastResult,
    required this.verdict,
  });
}

/// Top-level entry point tying the v2 on-device lineup together:
/// translatorA + translatorB attempt the same translation (pipeline mode —
/// sequential, since they share one on-device compute resource, same
/// reasoning as Kilonova's Python `_is_pipeline_mode`/`_pipeline_round`),
/// both write to the shared notebook, translatorA's result returns
/// immediately as the fast path, and the arbiter runs separately.
class ArtaxiaEngine {
  final LegRegistry registry;
  final ArbitrationNotebook notebook;
  final DotPayloadSource dotSource;

  ArtaxiaEngine({
    required this.registry,
    required this.notebook,
    required this.dotSource,
  });

  Future<TranslationTurn> translate({
    required String turnId,
    required String sourceText,
    required String sourceLanguageIso,
    required String targetLanguageIso,
  }) async {
    final translators = registry.translators;
    if (translators.length < 2) {
      throw StateError(
        'ArtaxiaEngine.translate requires two live translator legs, found '
        '${translators.length}.',
      );
    }

    final dot = await dotSource.loadActivePayload(
      sourceLanguageIso,
      targetLanguageIso,
      sourceText,
    );
    final taskPrompt = 'Translate the following text into $targetLanguageIso:'
        '\n\n$sourceText';
    final fullPrompt = DotCard(
      cardId: 'combined_$turnId',
      languageIso: targetLanguageIso,
      content: dot.combinedContent,
    ).wrap(taskPrompt);
    final messages = [ChatMessage(role: 'user', content: fullPrompt)];
    // ignore: avoid_print
    print('[ArtaxiaEngine] taskPrompt: "$taskPrompt"');
    // ignore: avoid_print
    print('[ArtaxiaEngine] fullPrompt (${fullPrompt.length} chars): $fullPrompt');

    // Sequential firing — pipeline mode, matches the Python engine's
    // reasoning: both legs share one on-device compute resource, so
    // "parallel" would just contend with itself rather than genuinely
    // overlap. First result becomes the fast path the caller sees now.
    final results = <String, String>{};
    for (final leg in translators.take(2)) {
      final legKey = leg.role == LegRole.translatorA
          ? 'translatorA'
          : 'translatorB';
      try {
        final text = await leg.provider.complete(
          // Explicit "output nothing else" instruction — a real, observed
          // failure mode without it: small models narrating their own
          // caveats/reasoning ("**Notes:** Do not translate word-for-word...")
          // as if that commentary were part of the answer, instead of
          // just returning the translation.
          system: 'You are a precise, faithful translator. Output ONLY the '
              'translated text and nothing else — no notes, no commentary, '
              'no explanation of your approach, no alternate phrasings.',
          messages: messages,
        );
        results[legKey] = text;
        notebook.write(
          turnId: turnId,
          legName: legKey,
          kind: 'translation',
          content: text,
        );
      } catch (e) {
        // Printed, not swallowed — a killed leg otherwise surfaces to the
        // caller only as "requires two live translator legs, found N", with
        // the actual root cause (model load failure, OOM, missing native
        // backend, etc.) lost. That gap cost real debugging time once
        // already; don't reintroduce it.
        print('[ArtaxiaEngine] leg "${leg.name}" failed and was killed: $e');
        registry.killLeg(leg.name, e.toString());
      }
    }

    final fastResult = results['translatorA'] ?? results['translatorB'];
    if (fastResult == null) {
      throw StateError('Both translator legs failed for turn "$turnId".');
    }

    // Arbitration is not real-time — this Future is handed back to the
    // caller to await whenever it's actually convenient, not blocked on
    // here. If only one translator succeeded there's nothing to arbitrate
    // between, so skip straight to a trivial agreeing verdict instead of
    // throwing (StateError inside arbitrate() expects both candidates).
    final verdictFuture = results.length == 2
        ? arbitrate(
            registry: registry,
            notebook: notebook,
            turnId: turnId,
            sourceText: sourceText,
            targetLanguageIso: targetLanguageIso,
          )
        : Future.value(
            ArbitrationVerdict(
              agree: true,
              bestTranslation: fastResult,
              disputedSpan: null,
              reasoning: 'Only one translator leg was available; no second '
                  'candidate to arbitrate against.',
              confidenceScore: 0.5,
            ),
          );

    return TranslationTurn(
      turnId: turnId,
      fastResult: fastResult,
      verdict: verdictFuture,
    );
  }
}
