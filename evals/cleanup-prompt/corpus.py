"""Built-in synthetic corpus for the cleanup-prompt eval. Offline, no network.

52 (disfluent, reference) pairs covering the failure classes Bolo's cleanup
prompt handles. Pattern absorbed from AssemblyAI/blurt's DSPy eval (scores
candidate cleanup instructions against a hand-annotated disfluency corpus,
Switchboard-derived), but the corpus here is synthetic and self-contained.

Field per pair:
  id         : stable identifier, class-prefixed (grep-friendly in results)
  class      : one of CLASSES below; the leaderboard breaks down by it
  disfluent  : what STT would plausibly emit (fillers, self-corrections,
               missing punctuation, lost apostrophes)
  reference  : what the speaker meant (the desired cleanup)

Class quotas: self_correction >= 10, false_positive_trap >= 6 (traps are
"no"/"actually" usages that must SURVIVE cleanup). Do not fetch the real
corpus (nyralabs/disfluency_speech_english) for v1; see README.md for the
upgrade path.
"""

CLASSES = [
    "filler",
    "self_correction",
    "false_positive_trap",
    "punctuation",
    "article_contraction",
    "multi_sentence",
    "edge_case",
]

CORPUS = [
    # --- filler removal: mid-sentence and trailing um/uh -------------------
    {
        "id": "fill-01",
        "class": "filler",
        "disfluent": "um can you send me the report when you get a chance",
        "reference": "Can you send me the report when you get a chance?",
    },
    {
        "id": "fill-02",
        "class": "filler",
        "disfluent": "I think we should uh start the meeting on time tomorrow",
        "reference": "I think we should start the meeting on time tomorrow.",
    },
    {
        "id": "fill-03",
        "class": "filler",
        "disfluent": "let me know when you're free uh",
        "reference": "Let me know when you're free.",
    },
    {
        "id": "fill-04",
        "class": "filler",
        "disfluent": "the um the deadline is next Friday",
        "reference": "The deadline is next Friday.",
    },
    {
        "id": "fill-05",
        "class": "filler",
        "disfluent": "I uh I already sent the invoice yesterday",
        "reference": "I already sent the invoice yesterday.",
    },
    {
        "id": "fill-06",
        "class": "filler",
        "disfluent": "we uh we need to reschedule the call",
        "reference": "We need to reschedule the call.",
    },
    {
        "id": "fill-07",
        "class": "filler",
        "disfluent": "the survey results were uh surprising",
        "reference": "The survey results were surprising.",
    },
    # --- self-corrections: the "no wait, actually" revision class ---------
    {
        "id": "corr-01",
        "class": "self_correction",
        "disfluent": "the event is on September 15th no wait actually it's on October 15th",
        "reference": "The event is on October 15th.",
    },
    {
        "id": "corr-02",
        "class": "self_correction",
        "disfluent": "let's meet on Tuesday no actually Wednesday at noon",
        "reference": "Let's meet on Wednesday at noon.",
    },
    {
        "id": "corr-03",
        "class": "self_correction",
        "disfluent": "I paid two hundred dollars no sorry three hundred dollars for the repair",
        "reference": "I paid three hundred dollars for the repair.",
    },
    {
        "id": "corr-04",
        "class": "self_correction",
        "disfluent": "the code review is with Priya no wait it's with Marcus",
        "reference": "The code review is with Marcus.",
    },
    {
        "id": "corr-05",
        "class": "self_correction",
        "disfluent": "my flight lands at 6 PM no actually 7 PM",
        "reference": "My flight lands at 7 PM.",
    },
    {
        "id": "corr-06",
        "class": "self_correction",
        "disfluent": "the password is the dog's name no wait it's my birthday",
        "reference": "The password is my birthday.",
    },
    {
        "id": "corr-07",
        "class": "self_correction",
        "disfluent": "we shipped the feature to the beta channel no actually we shipped it to everyone",
        "reference": "We shipped the feature to everyone.",
    },
    {
        "id": "corr-08",
        "class": "self_correction",
        "disfluent": "I ordered the large size no hold on the medium",
        "reference": "I ordered the medium size.",
    },
    {
        "id": "corr-09",
        "class": "self_correction",
        "disfluent": "the doctor's appointment is at 9 no wait it moved to 10 30",
        "reference": "The doctor's appointment is at 10:30.",
    },
    {
        "id": "corr-10",
        "class": "self_correction",
        "disfluent": "our office is in Austin no actually it's in Denver now",
        "reference": "Our office is in Denver now.",
    },
    {
        "id": "corr-11",
        "class": "self_correction",
        "disfluent": "the budget is forty thousand no wait fifty thousand for the quarter",
        "reference": "The budget is fifty thousand for the quarter.",
    },
    {
        "id": "corr-12",
        "class": "self_correction",
        "disfluent": "send it to the marketing channel no wait the engineering channel",
        "reference": "Send it to the engineering channel.",
    },
    # --- false-positive traps: "no"/"actually" that must SURVIVE ----------
    {
        "id": "trap-01",
        "class": "false_positive_trap",
        "disfluent": "she said no to the first offer actually she came back with a counteroffer",
        "reference": "She said no to the first offer. Actually, she came back with a counteroffer.",
    },
    {
        "id": "trap-02",
        "class": "false_positive_trap",
        "disfluent": "the answer is no and that's final",
        "reference": "The answer is no, and that's final.",
    },
    {
        "id": "trap-03",
        "class": "false_positive_trap",
        "disfluent": "if it asks you to overwrite say no",
        "reference": "If it asks you to overwrite, say no.",
    },
    {
        "id": "trap-04",
        "class": "false_positive_trap",
        "disfluent": "uh there's no way we can finish this by Friday",
        "reference": "There's no way we can finish this by Friday.",
    },
    {
        "id": "trap-05",
        "class": "false_positive_trap",
        "disfluent": "the project wrapped up on time actually we finished two days early",
        "reference": "The project wrapped up on time. Actually, we finished two days early.",
    },
    {
        "id": "trap-06",
        "class": "false_positive_trap",
        "disfluent": "the client said no to the timeline change",
        "reference": "The client said no to the timeline change.",
    },
    {
        "id": "trap-07",
        "class": "false_positive_trap",
        "disfluent": "the demo actually went better than expected",
        "reference": "The demo actually went better than expected.",
    },
    {
        "id": "trap-08",
        "class": "false_positive_trap",
        "disfluent": "we have no more copies left um in the office",
        "reference": "We have no more copies left in the office.",
    },
    # --- punctuation and capitalization restoration ------------------------
    {
        "id": "punct-01",
        "class": "punctuation",
        "disfluent": "can you review the pull request before standup",
        "reference": "Can you review the pull request before standup?",
    },
    {
        "id": "punct-02",
        "class": "punctuation",
        "disfluent": "i will be out of the office next week please forward anything urgent to Maria",
        "reference": "I will be out of the office next week. Please forward anything urgent to Maria.",
    },
    {
        "id": "punct-03",
        "class": "punctuation",
        "disfluent": "let's grab lunch at noon it's my treat",
        "reference": "Let's grab lunch at noon. It's my treat.",
    },
    {
        "id": "punct-04",
        "class": "punctuation",
        "disfluent": "hey did you see the email from Sarah",
        "reference": "Hey, did you see the email from Sarah?",
    },
    {
        "id": "punct-05",
        "class": "punctuation",
        "disfluent": "we tried three approaches but only the third one worked",
        "reference": "We tried three approaches, but only the third one worked.",
    },
    {
        "id": "punct-06",
        "class": "punctuation",
        "disfluent": "well I guess we will find out tomorrow",
        "reference": "Well, I guess we will find out tomorrow.",
    },
    # --- missing articles and lost contractions ---------------------------
    {
        "id": "artc-01",
        "class": "article_contraction",
        "disfluent": "we went to store after lunch",
        "reference": "We went to the store after lunch.",
    },
    {
        "id": "artc-02",
        "class": "article_contraction",
        "disfluent": "she is best person for the job",
        "reference": "She is the best person for the job.",
    },
    {
        "id": "artc-03",
        "class": "article_contraction",
        "disfluent": "im running late to the airport",
        "reference": "I'm running late to the airport.",
    },
    {
        "id": "artc-04",
        "class": "article_contraction",
        "disfluent": "ill call you when I land",
        "reference": "I'll call you when I land.",
    },
    {
        "id": "artc-05",
        "class": "article_contraction",
        "disfluent": "were going to try the new Thai place tonight",
        "reference": "We're going to try the new Thai place tonight.",
    },
    {
        "id": "artc-06",
        "class": "article_contraction",
        "disfluent": "shell be in the office by ten",
        "reference": "She'll be in the office by ten.",
    },
    # --- multi-sentence dictations with topic moves ------------------------
    {
        "id": "mult-01",
        "class": "multi_sentence",
        "disfluent": "the interview went well the candidate knew the stack inside out um next we should check references before Friday",
        "reference": "The interview went well. The candidate knew the stack inside out. Next, we should check references before Friday.",
    },
    {
        "id": "mult-02",
        "class": "multi_sentence",
        "disfluent": "I finished the report last night also I updated the shared dashboard with the new numbers",
        "reference": "I finished the report last night. Also, I updated the shared dashboard with the new numbers.",
    },
    {
        "id": "mult-03",
        "class": "multi_sentence",
        "disfluent": "the printer upstairs is broken again maintenance said they'd look at it tomorrow oh and we have a new intern starting Monday",
        "reference": "The printer upstairs is broken again. Maintenance said they'd look at it tomorrow. Oh, and we have a new intern starting Monday.",
    },
    {
        "id": "mult-04",
        "class": "multi_sentence",
        "disfluent": "traffic was terrible this morning so I took the bridge instead anyway the meeting notes are in the shared folder",
        "reference": "Traffic was terrible this morning, so I took the bridge instead. Anyway, the meeting notes are in the shared folder.",
    },
    {
        "id": "mult-05",
        "class": "multi_sentence",
        "disfluent": "first preheat the oven to 375 then mix the dry ingredients finally fold in the chocolate chips",
        "reference": "First, preheat the oven to 375. Then mix the dry ingredients. Finally, fold in the chocolate chips.",
    },
    {
        "id": "mult-06",
        "class": "multi_sentence",
        "disfluent": "we covered onboarding in the last retro the next topic is the deployment pipeline and we should timebox it to thirty minutes",
        "reference": "We covered onboarding in the last retro. The next topic is the deployment pipeline, and we should timebox it to thirty minutes.",
    },
    # --- edge cases: caps, numbers, non-English words, near-empty ----------
    {
        "id": "edge-01",
        "class": "edge_case",
        "disfluent": "the vendor call is uh with IBM on Thursday",
        "reference": "The vendor call is with IBM on Thursday.",
    },
    {
        "id": "edge-02",
        "class": "edge_case",
        "disfluent": "the reservation is for 7 people at 7 PM on Saturday",
        "reference": "The reservation is for 7 people at 7 PM on Saturday.",
    },
    {
        "id": "edge-03",
        "class": "edge_case",
        "disfluent": "my grandmother calls me beta at home it means dear in Hindi",
        "reference": "My grandmother calls me beta at home. It means dear in Hindi.",
    },
    {
        "id": "edge-04",
        "class": "edge_case",
        "disfluent": "yeah",
        "reference": "Yeah.",
    },
    {
        "id": "edge-05",
        "class": "edge_case",
        "disfluent": "thanks",
        "reference": "Thanks.",
    },
    {
        "id": "edge-06",
        "class": "edge_case",
        "disfluent": "the conference runs from June 1st through June 5th",
        "reference": "The conference runs from June 1st through June 5th.",
    },
    {
        "id": "edge-07",
        "class": "edge_case",
        "disfluent": "we ordered um naan and butter chicken for the team lunch",
        "reference": "We ordered naan and butter chicken for the team lunch.",
    },
]
