/// Base interface every on-device inference provider must implement.
///
/// Mirrors Kilonova's Python `BaseProvider` shape (`core/providers/base.py`)
/// deliberately, so behavior stays easy to reason about across both
/// orchestrator implementations. See the standing "keep both in sync"
/// reminder in project memory before changing either shape.
abstract class InferenceProvider {
  /// Human-readable name, e.g. 'qwen2.5-1.5b'.
  String get name;

  /// Run one complete (non-streaming) inference call.
  Future<String> complete({
    required String system,
    required List<ChatMessage> messages,
  });

  /// Release any held native resources (loaded model, native context).
  /// Default no-op — only providers backed by a real on-device engine
  /// (e.g. the arbiter leg, which is deliberately non-persistent so its
  /// model isn't resident in memory between turns) need to override this.
  Future<void> dispose() async {}
}

class ChatMessage {
  final String role; // 'user' | 'assistant'
  final String content;

  const ChatMessage({required this.role, required this.content});
}
