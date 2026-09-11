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

## V2 / V3 — home server

Optional split: this laptop still records and can transcribe, but storage,
search, and the local LLM can run on a higher-power machine on the home
network.

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

- Installer for laptop, or laptop + home server
- Local capture, local transcription, local search
- Buyer chooses: this computer, or their own server
- Not a Claude/ChatGPT wrapper; those stay optional query clients

Current non-negotiables that should survive packaging: no automatic recording,
no silent speaker naming, no required cloud account for the core loop.
