import 'providers/base.dart';

/// The three roles in the v2 on-device lineup — see project memory
/// "Final model lineup v2" for the real, verified model choices and why
/// each one has this specific job:
///   - translatorA / translatorB: two genuinely different models attempt
///     the same translation independently (not one model sampled twice —
///     real differing approaches, so disagreement between them is
///     meaningful signal, not sampling noise).
///   - arbiter: reads both attempts, judges which is correct.
enum LegRole { translatorA, translatorB, arbiter }

class Leg {
  final String name;
  final LegRole role;
  final InferenceProvider provider;
  bool alive;
  String? error;

  Leg({
    required this.name,
    required this.role,
    required this.provider,
    this.alive = true,
    this.error,
  });
}

/// Fixed on-device leg registry.
///
/// Unlike Kilonova's Python `LegRegistry` (`core/legs.py`), which
/// dynamically discovers cloud providers from stored API keys at startup,
/// this is a known, fixed set of exactly three local models — there is
/// nothing to discover on-device, so this is deliberately much simpler
/// than the Python version it's inspired by.
class LegRegistry {
  final Map<String, Leg> legs;

  LegRegistry(this.legs);

  factory LegRegistry.fromLegs(List<Leg> legList) {
    return LegRegistry({for (final leg in legList) leg.name: leg});
  }

  List<Leg> get translators => legs.values
      .where((l) => l.alive && l.role != LegRole.arbiter)
      .toList(growable: false);

  Leg? get arbiter {
    for (final leg in legs.values) {
      if (leg.alive && leg.role == LegRole.arbiter) return leg;
    }
    return null;
  }

  /// Mark a leg dead — called when a provider fails outright (not a
  /// transient error). Mirrors `LegRegistry.kill_leg` in the Python engine.
  void killLeg(String name, String reason) {
    final leg = legs[name];
    if (leg != null) {
      leg.alive = false;
      leg.error = reason;
    }
  }
}
