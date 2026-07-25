/// Milna OS: Artaxia — the on-device edition of the Kilonova orchestration
/// engine. Pure Dart, headless (no UI of its own — see project memory:
/// this is deliberately a runtime, not a product with its own interface,
/// same relationship the Python Kilonova engine has to Milna OS's
/// terminal).
library;

export 'src/arbiter.dart';
export 'src/dot.dart';
export 'src/engine.dart';
export 'src/legs.dart';
export 'src/notebook.dart';
export 'src/providers/base.dart';

// No concrete InferenceProvider implementation is exported from this
// package on purpose — this package is pure Dart with no Flutter
// dependency, and every real inference backend (llm_llamacpp, or anything
// else) requires the Flutter SDK for native platform bindings. The
// consuming app implements InferenceProvider itself, the same way it
// implements DotPayloadSource, and hands the implementation to
// ArtaxiaEngine at construction time. Keeps this package genuinely
// headless and not locked into one specific inference binding.
