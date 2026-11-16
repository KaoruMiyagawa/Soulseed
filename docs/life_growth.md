# Life growth

The first growth pass reuses `life_preferences` and `life_preference_changes`; it adds no habit table or scheduler. When LifeEngine completes a saved activity with a recorded positive or negative feeling, it nudges that activity's existing weight. The same weight remains the input to the existing activity chooser.

`LifeRepository.preference_growth()` is a read-only, character-scoped view over completed events and their `completed_feeling` weight changes. A single event is never presented as a lasting interest. Repeated changes are summarized as forming, established, waning, recovering, cooling, or mixed. The summary retains recent direction, so a character can change over time. The current mood remains separate and temporary.

ChatService and the shared proactive expression context receive this summary. Autonomous social decisions receive the same role-owned summary alongside mood, activity, user memories, and interaction context. User memories are not used to infer the character's own interests. The persona's stable traits remain in the existing persona configuration; growth adds experience-based tendencies without replacing them.

The summary is bounded to recent saved preference changes and is re-derived after restart. This first pass does not infer habits from unsaved model text or user preferences.
