/// One entry in the shared arbitration notebook.
///
/// Once [locked] is true the entry is finalized and must never be mutated
/// again — the "read only flag" from the design conversation: a study
/// session notebook where nobody scribbles over what's already been
/// written down.
class NotebookEntry {
  final String id;
  final String legName;
  final String kind; // 'translation' | 'verdict'
  final String content;
  final DateTime writtenAt;
  bool locked;

  NotebookEntry({
    required this.id,
    required this.legName,
    required this.kind,
    required this.content,
    required this.writtenAt,
    this.locked = false,
  });
}

/// Shared, indexed notebook both translators write into and the arbiter
/// reads from.
///
/// Existed for one explicit purpose — context management — motivated
/// specifically by arbitration not being real-time: translators finish and
/// write their attempt out, the arbiter reads and judges whenever it
/// actually gets to it, decoupled in time rather than a live synchronous
/// relay held in-context.
///
/// Retrieval is scoped by [turnId] specifically, so the arbiter only ever
/// pulls the current turn's candidates — never the whole session history.
/// That scoping is what actually keeps context bounded. An earlier design
/// (a rolling N-entry FIFO eviction window) was considered and correctly
/// dropped once this was clarified: scoped-by-turn retrieval already
/// prevents unbounded context growth, so a separate eviction policy on top
/// would just be redundant complexity.
///
/// Translators never read this — only the arbiter does. Qwen2.5 and
/// DeepSeek-R1 translate using the shared language DOT and write their
/// attempt here; they have no reason to consult past entries.
class ArbitrationNotebook {
  final Map<String, List<NotebookEntry>> _byTurn = {};

  /// Write one entry for [turnId]. Throws if an entry with the same
  /// (turnId, legName, kind) already exists and is locked — a locked entry
  /// is final, matching the "nobody overwrites what's already written"
  /// rule this whole mechanism exists to enforce.
  void write({
    required String turnId,
    required String legName,
    required String kind,
    required String content,
  }) {
    final id = '$turnId:$legName:$kind';
    final existing = _byTurn[turnId]?.where((e) => e.id == id).firstOrNull;
    if (existing != null && existing.locked) {
      throw StateError(
        'Notebook entry "$id" is locked (read-only) and cannot be '
        'rewritten.',
      );
    }
    final entry = NotebookEntry(
      id: id,
      legName: legName,
      kind: kind,
      content: content,
      writtenAt: DateTime.now(),
    );
    _byTurn.putIfAbsent(turnId, () => []);
    _byTurn[turnId]!.removeWhere((e) => e.id == id);
    _byTurn[turnId]!.add(entry);
  }

  /// Locks every entry currently on record for [turnId]. Call once both
  /// translators have finished writing, before the arbiter reads — after
  /// this, [write] for the same (turnId, legName, kind) will throw.
  void lockTurn(String turnId) {
    for (final entry in _byTurn[turnId] ?? const <NotebookEntry>[]) {
      entry.locked = true;
    }
  }

  /// Scoped read — only this turn's entries, never the full notebook
  /// history. This is the mechanism that actually bounds context, not a
  /// size limit.
  List<NotebookEntry> read(String turnId) =>
      List.unmodifiable(_byTurn[turnId] ?? const <NotebookEntry>[]);

  /// Drop a turn's entries once arbitration is complete and nothing needs
  /// them anymore. Safe to skip — the notebook is plain text and trivially
  /// small even unpruned (see project memory: never a meaningful storage
  /// contributor) — but keeps long-running sessions tidy.
  void clearTurn(String turnId) => _byTurn.remove(turnId);
}
