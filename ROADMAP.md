# Roadmap

Not scheduled work. Intentional later versions, written down so they are not
lost in chat.

## V2 — better guesses for Speaker 1, 2, 3…

Today a voice is distinguished (room-1, room-2) and only labelled with a real
name after Jacob confirms it. Intros and handoffs already *suggest* a name for
the dialog. V2 should make those suggestions good enough that “who was Speaker
2?” is usually pre-filled correctly, still never applied until confirmed.

Ideas already in the brief, plus the ones Jacob wants remembered:

- Stronger use of self-introductions and handoffs (“I’m Jacob, I’ll pass to
  John”) so the next distinct voice is guessed as John, not a new unknown.
- Keep a local roster of voices Jacob has named before, so a returning person
  can be suggested from how they sound, not only from this meeting’s words.
- Use what is already known from the channel: on a call the mic is Jacob;
  guesses are for everyone else. In a room, do not assume the first speaker is
  Jacob.
- Better quips so the popup is easy to match (“this person talked about the
  house renovation”) when the guess is still wrong.
- If Jacob typed attendees (AJ, Tony) and the model found fewer voices, help
  him map names to the detected voices instead of inventing extras.
- Never write a guessed name into the transcript until Jacob saves it. A
  better guess is still a guess.

Add more of Jacob’s ideas here as they come.

### Where V2 stands (November 2026)

Most of the list above is built. Written down so nobody builds it twice:

- Returning-voice roster: done. `speakers/library.json` keeps an averaged
  voiceprint and a clip per confirmed person; `recognise_known` suggests them.
- Typed attendees narrowing suggestions: done. `usable_dialog_guess` and
  `dialog_prediction` only pre-fill a listed person, and a near-tie stays blank.
- Mapping the last listed name to the last unnamed voice: done
  (`leftover_assignment`), and the user is told.
- Several quotes and a playable clip per voice: done (`identity_clips`).
- Junk speakers: done. Under `min_participant_s` (3s) a voice is not a
  participant; see `settle_brief_voices` in `diarize.py`.

Still open, in the order worth doing:

1. **Split a sentence at a speaker change.** Whisper's segments are still
   authoritative, so a segment that straddles two people goes whole to
   whoever holds more of it. Word timestamps would let a clean change of
   speaker inside one segment be cut at the word. Needs checking that
   whisper.cpp's word times are steady enough on real calls first.
2. **Several voiceprints per person, not one average.** Someone on a
   laptop mic and the same person on a headset are averaged into a print
   that matches neither well. Keep up to a few prints per person and match
   the closest. Needs real cross-meeting audio to set the bar.
3. **Stronger handoffs.** "Over to you, John" should weight the next new
   voice as John more than it does now.

## Next for the Mac app

Ideas from the November 2026 polish pass, ranked by how much they would
change a normal day against how much work they are.

1. **Search and read inside the app.** The library, transcripts and brief
   open in a browser. A native list with search-as-you-type over the
   existing index would make Yarn one window instead of three.
2. **Export a meeting.** Markdown already exists on disk; add "Export" for
   plain text and PDF with speakers and timestamps, from the app.
3. **Correct a line.** Edit a transcript line or reassign its speaker, save
   to the JSON, reindex that one meeting. The JSON stays the record.
4. **Glossary in Settings.** Done. One line per word (`heard = correct`) in
   Settings. `[glossary]` in config.toml still applies until that save.
5. **Audio on pages rendered on demand.** Done. The library server plays a
   room recording, and both sides of a headphone call.
6. **Recovery in the app.** Stranded and failed recordings are listed in the
   menu bar and fixed with `ma recover`. A "Needs attention" row in the main
   window with Publish and Discard would mean never opening Terminal.
7. **A chosen shortcut.** The record shortcut is fixed at
   Control-Option-Command-R. Let it be recorded in Settings.

## Windows

See [docs/PLATFORM_BOUNDARY.md](docs/PLATFORM_BOUNDARY.md) for where the
Mac-only code is today and the boundary to cut along. Not started.

## V2 / V3 — home server (opt-in)

The laptop always records and, by default, processes locally. A home server
is later, for people who have one: storage, search, and heavier Whisper can
move there. It is not the first option and not required.

Optional split when it exists: this laptop still records, but storage, search,
and the local LLM can run on a higher-power machine on the home network.

- Archive root (transcripts, Chroma, audio retention) on the server
- Query from Claude Desktop / a small client against that remote index
- Summaries and heavier Whisper on the server’s GPU or extra RAM
- Same privacy constraint: nothing leaves the house unless Jacob says so

The current design already keeps transcripts as the source of truth and Chroma
as rebuildable derived state, which is what makes this split possible without
rewriting the pipeline.

## V4 — package and sell

A one-time-fee product for people who want this workflow and do not want a
subscription to a large hosted model.

Mac first: `bin/yarn-setup` is the Hermes-style window. GitHub Releases will
be the public download (a `.dmg` wrapping that same wizard). Windows later.

- Installer for laptop (required); home server is an optional extra
- Local capture, local transcription, local search
- Buyer chooses: this computer, or their own server
- Not a Claude/ChatGPT wrapper; those stay optional query clients

Current non-negotiables that should survive packaging: no automatic recording,
no silent speaker naming, no required cloud account for the core loop.
