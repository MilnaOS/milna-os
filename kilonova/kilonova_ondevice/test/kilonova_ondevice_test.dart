import 'package:kilonova_ondevice/kilonova_ondevice.dart';
import 'package:test/test.dart';

/// Fake provider for tests — no real model call, just returns a fixed
/// response (or throws, if [shouldFail] is set) so the orchestration logic
/// can be exercised without needing an actual on-device model.
class FakeProvider implements InferenceProvider {
  @override
  final String name;
  final String response;
  final bool shouldFail;

  FakeProvider(this.name, this.response, {this.shouldFail = false});

  @override
  Future<String> complete({
    required String system,
    required List<ChatMessage> messages,
  }) async {
    if (shouldFail) throw Exception('fake failure for $name');
    return response;
  }

  @override
  Future<void> dispose() async {}
}

class FakeDotSource implements DotPayloadSource {
  @override
  Future<DotPayload> loadActivePayload(
    String sourceIso,
    String targetIso, [
    String queryText = '',
  ]) async {
    return DotPayload(
      sourceLanguageIso: sourceIso,
      targetLanguageIso: targetIso,
      sourceCard: DotCard(
        cardId: 'card_source_$sourceIso',
        languageIso: sourceIso,
        content: 'fake source grammar for $sourceIso',
      ),
      targetCard: DotCard(
        cardId: 'card_target_$targetIso',
        languageIso: targetIso,
        content: 'fake target grammar for $targetIso',
      ),
    );
  }
}

void main() {
  group('ArbitrationNotebook', () {
    test('writes and reads scoped by turn', () {
      final notebook = ArbitrationNotebook();
      notebook.write(
        turnId: 't1',
        legName: 'translatorA',
        kind: 'translation',
        content: 'hola',
      );
      notebook.write(
        turnId: 't2',
        legName: 'translatorA',
        kind: 'translation',
        content: 'bonjour',
      );

      final t1Entries = notebook.read('t1');
      expect(t1Entries, hasLength(1));
      expect(t1Entries.first.content, 'hola');

      // Scoped retrieval — t2's entry never leaks into a t1 read.
      expect(t1Entries.any((e) => e.content == 'bonjour'), isFalse);
    });

    test('locked entries cannot be overwritten', () {
      final notebook = ArbitrationNotebook();
      notebook.write(
        turnId: 't1',
        legName: 'translatorA',
        kind: 'translation',
        content: 'hola',
      );
      notebook.lockTurn('t1');

      expect(
        () => notebook.write(
          turnId: 't1',
          legName: 'translatorA',
          kind: 'translation',
          content: 'overwrite attempt',
        ),
        throwsStateError,
      );
    });

    test('clearTurn removes only that turn', () {
      final notebook = ArbitrationNotebook();
      notebook.write(
        turnId: 't1',
        legName: 'translatorA',
        kind: 'translation',
        content: 'hola',
      );
      notebook.write(
        turnId: 't2',
        legName: 'translatorA',
        kind: 'translation',
        content: 'bonjour',
      );
      notebook.clearTurn('t1');

      expect(notebook.read('t1'), isEmpty);
      expect(notebook.read('t2'), hasLength(1));
    });
  });

  group('ArtaxiaEngine', () {
    test('returns a fast result as soon as translatorA finishes, arbitrates separately', () async {
      final registry = LegRegistry.fromLegs([
        Leg(
          name: 'translatorA',
          role: LegRole.translatorA,
          provider: FakeProvider('translatorA', 'hola'),
        ),
        Leg(
          name: 'translatorB',
          role: LegRole.translatorB,
          provider: FakeProvider('translatorB', 'hola'),
        ),
        Leg(
          name: 'arbiter',
          role: LegRole.arbiter,
          provider: FakeProvider(
            'arbiter',
            '{"agree": true, "bestTranslation": "hola", '
                '"disputedSpan": null, "reasoning": "candidates match", '
                '"confidenceScore": 0.95}',
          ),
        ),
      ]);

      final engine = ArtaxiaEngine(
        registry: registry,
        notebook: ArbitrationNotebook(),
        dotSource: FakeDotSource(),
      );

      final turn = await engine.translate(
        turnId: 'test-turn-1',
        sourceText: 'hello',
        sourceLanguageIso: 'eng',
        targetLanguageIso: 'spa',
      );

      expect(turn.fastResult, 'hola');

      final verdict = await turn.verdict;
      expect(verdict.agree, isTrue);
      expect(verdict.bestTranslation, 'hola');
    });

    test('parses a real observed malformed arbiter response (doubled braces + leaked chat-template tokens)', () async {
      // Captured verbatim from a real on-device run (Granite 4.1 3B Q8_0,
      // 2026-07-19) — the naive first-`{`-to-last-`}` extractor choked on
      // this and silently fell back to defaults (agree=false,
      // confidenceScore=0.5, bestTranslation=candidateA), masking a real
      // verdict the model had actually produced correctly.
      const malformed = 'assistant{{"agree": false, "bestTranslation": null, '
          '"disputedSpan": null, "reasoning": "Candidate B is nonsensical.", '
          '"confidenceScore": 0.95}}<|im_end|>';

      final registry = LegRegistry.fromLegs([
        Leg(name: 'translatorA', role: LegRole.translatorA, provider: FakeProvider('translatorA', 'hola')),
        Leg(name: 'translatorB', role: LegRole.translatorB, provider: FakeProvider('translatorB', 'garbage')),
        Leg(name: 'arbiter', role: LegRole.arbiter, provider: FakeProvider('arbiter', malformed)),
      ]);

      final engine = ArtaxiaEngine(
        registry: registry,
        notebook: ArbitrationNotebook(),
        dotSource: FakeDotSource(),
      );

      final turn = await engine.translate(
        turnId: 'test-turn-malformed',
        sourceText: 'hello',
        sourceLanguageIso: 'eng',
        targetLanguageIso: 'spa',
      );
      final verdict = await turn.verdict;

      // The real confidenceScore (0.95) and reasoning must survive — if
      // extraction had fallen back to defaults, confidenceScore would be
      // the hardcoded 0.5 instead.
      expect(verdict.confidenceScore, 0.95);
      expect(verdict.agree, isFalse);
      expect(verdict.reasoning, 'Candidate B is nonsensical.');
    });

    test('falls back to the surviving translator if one leg fails', () async {
      final registry = LegRegistry.fromLegs([
        Leg(
          name: 'translatorA',
          role: LegRole.translatorA,
          provider: FakeProvider('translatorA', 'hola', shouldFail: true),
        ),
        Leg(
          name: 'translatorB',
          role: LegRole.translatorB,
          provider: FakeProvider('translatorB', 'hola'),
        ),
        Leg(
          name: 'arbiter',
          role: LegRole.arbiter,
          provider: FakeProvider('arbiter', '{}'),
        ),
      ]);

      final engine = ArtaxiaEngine(
        registry: registry,
        notebook: ArbitrationNotebook(),
        dotSource: FakeDotSource(),
      );

      final turn = await engine.translate(
        turnId: 'test-turn-2',
        sourceText: 'hello',
        sourceLanguageIso: 'eng',
        targetLanguageIso: 'spa',
      );

      expect(turn.fastResult, 'hola');
      // Only one real candidate — arbitration is skipped with a trivial
      // agreeing verdict rather than throwing.
      final verdict = await turn.verdict;
      expect(verdict.agree, isTrue);
    });
  });
}
