//! Context-aware repair of misheard names, run after paste.
//! The LLM only proposes `{span, term}` swaps; this module decides which
//! ones apply. A swap survives only when the term is a vocabulary term or a
//! name visible on screen, the span sits in the transcript verbatim as whole
//! words (at most three), and span and term sound alike: letters-only
//! Levenshtein over the longer side of at most 0.5, digits read as words so
//! "10x" can reach "Telnyx". Everything else stays exactly as dictated.
//! Offline eval (2026-11-01, 86 Willow fixes + 300 unedited transcripts):
//! zero wrong swaps survived this gate on the unedited set.

/// System prompt for the repair call. The examples are real mishearings
/// from the speaker's own dictation history.
#[cfg_attr(test, allow(dead_code))]
pub(crate) const PROMPT: &str = "You repair misheard names in a voice dictation transcript.
You get the transcript, the speaker's vocabulary (names, products, tools they use), and text visible on their screen.
Speech recognition sometimes turns a name into similar-sounding ordinary words, e.g. \"Ormis\" for \"Hermes\", \"below voice\" for \"Willow Voice\", \"10x\" for \"Telnyx\".
Return JSON only: {\"fixes\":[{\"span\":\"<exact substring of the transcript>\",\"term\":\"<vocabulary or on-screen term>\"}]}
Rules:
- Only fix a span that sounds like a vocabulary or on-screen term AND makes little sense as spoken in this sentence.
- span must be copied exactly from the transcript, 1 to 3 words.
- Never rephrase, never fix grammar or punctuation, never add or remove other words.
- Most transcripts need no fixes: then return {\"fixes\":[]}.";

/// Longest span, in words, a swap may replace.
const MAX_SPAN_WORDS: usize = 3;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Fix {
    pub(crate) span: String,
    pub(crate) term: String,
}

/// Pull the proposed fixes out of a model reply. Anything that is not the
/// documented JSON shape yields no fixes.
pub(crate) fn parse_fixes(output: &str) -> Vec<Fix> {
    let (Some(start), Some(end)) = (output.find('{'), output.rfind('}')) else {
        return Vec::new();
    };
    if end < start {
        return Vec::new();
    }
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&output[start..=end]) else {
        return Vec::new();
    };
    value
        .get("fixes")
        .and_then(serde_json::Value::as_array)
        .map(|fixes| {
            fixes
                .iter()
                .filter_map(|fix| {
                    Some(Fix {
                        span: fix.get("span")?.as_str()?.to_owned(),
                        term: fix.get("term")?.as_str()?.to_owned(),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Names on screen a swap may target: tokens that carry a capital letter
/// ("Hermes", "OpenChamber") and are not ordinary words, so a capitalized
/// sentence opener like "The" never becomes a swap target.
pub(crate) fn screen_terms(screen: &str, is_common_word: impl Fn(&str) -> bool) -> Vec<String> {
    let mut terms = Vec::new();
    for token in screen.split(|character: char| {
        !(character.is_alphanumeric() || character == '-' || character == '.')
    }) {
        let token = token.trim_matches(|character: char| character == '.' || character == '-');
        if token.chars().count() < 3
            || !token.chars().next().is_some_and(char::is_alphabetic)
            || !token.chars().any(char::is_uppercase)
            || is_common_word(token)
            || terms.iter().any(|term: &String| term == token)
        {
            continue;
        }
        terms.push(token.to_owned());
    }
    terms
}

/// Keep only the fixes that clear every gate, with each term spelled the way
/// the vocabulary or screen spells it.
pub(crate) fn validate(text: &str, fixes: &[Fix], allowed_terms: &[String]) -> Vec<Fix> {
    let mut accepted: Vec<Fix> = Vec::new();
    for fix in fixes {
        let Some(term) = allowed_terms
            .iter()
            .find(|allowed| allowed.eq_ignore_ascii_case(fix.term.trim()))
        else {
            continue;
        };
        let span = fix.span.trim();
        if span.is_empty()
            || span == term
            || span.split_whitespace().count() > MAX_SPAN_WORDS
            || find_whole(text, span).is_none()
            || !sounds_alike(span, term)
            || accepted.iter().any(|existing| existing.span == span)
        {
            continue;
        }
        accepted.push(Fix {
            span: span.to_owned(),
            term: term.clone(),
        });
    }
    accepted
}

/// Apply accepted fixes to every whole-word occurrence of each span.
pub(crate) fn apply(text: &str, fixes: &[Fix]) -> String {
    let mut result = text.to_owned();
    for fix in fixes {
        let mut rebuilt = String::with_capacity(result.len());
        let mut rest = result.as_str();
        while let Some(index) = find_whole(rest, &fix.span) {
            rebuilt.push_str(&rest[..index]);
            rebuilt.push_str(&fix.term);
            rest = &rest[index + fix.span.len()..];
        }
        rebuilt.push_str(rest);
        result = rebuilt;
    }
    result
}

/// Byte index of the first occurrence of `needle` that starts and ends on a
/// word boundary.
fn find_whole(haystack: &str, needle: &str) -> Option<usize> {
    if needle.is_empty() {
        return None;
    }
    let is_word = |character: Option<char>| character.is_some_and(char::is_alphanumeric);
    let mut offset = 0;
    while let Some(found) = haystack[offset..].find(needle) {
        let start = offset + found;
        let end = start + needle.len();
        if !is_word(haystack[..start].chars().next_back())
            && !is_word(haystack[end..].chars().next())
        {
            return Some(start);
        }
        offset = start + needle.chars().next().map_or(1, char::len_utf8);
    }
    None
}

/// Whether a spoken span plausibly is a mishearing of `term`.
pub(crate) fn sounds_alike(span: &str, term: &str) -> bool {
    let spoken = spoken_letters(span);
    let target = spoken_letters(term);
    if spoken.is_empty() || target.is_empty() {
        return false;
    }
    let longest = spoken.len().max(target.len());
    levenshtein(&spoken, &target) * 2 <= longest
}

/// Lowercase ASCII letters with digit runs read as English words.
fn spoken_letters(text: &str) -> Vec<char> {
    const WORDS: [&str; 21] = [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
    ];
    let mut letters = Vec::new();
    let mut digits = String::new();
    let flush = |digits: &mut String, letters: &mut Vec<char>| {
        if digits.is_empty() {
            return;
        }
        match digits
            .parse::<usize>()
            .ok()
            .and_then(|number| WORDS.get(number))
        {
            Some(word) => letters.extend(word.chars()),
            None => {
                for digit in digits.chars() {
                    let index = digit.to_digit(10).unwrap_or_default() as usize;
                    letters.extend(WORDS[index].chars());
                }
            }
        }
        digits.clear();
    };
    for character in text.chars() {
        if character.is_ascii_digit() {
            digits.push(character);
            continue;
        }
        flush(&mut digits, &mut letters);
        if character.is_ascii_alphabetic() {
            letters.push(character.to_ascii_lowercase());
        }
    }
    flush(&mut digits, &mut letters);
    letters
}

fn levenshtein(left: &[char], right: &[char]) -> usize {
    let mut previous = (0..=right.len()).collect::<Vec<_>>();
    let mut current = vec![0; right.len() + 1];
    for (left_index, left_char) in left.iter().enumerate() {
        current[0] = left_index + 1;
        for (right_index, right_char) in right.iter().enumerate() {
            current[right_index + 1] = (previous[right_index + 1] + 1)
                .min(current[right_index] + 1)
                .min(previous[right_index] + usize::from(left_char != right_char));
        }
        std::mem::swap(&mut previous, &mut current);
    }
    previous[right.len()]
}

#[cfg(test)]
mod tests {
    use super::{Fix, apply, parse_fixes, screen_terms, sounds_alike, validate};

    fn fix(span: &str, term: &str) -> Fix {
        Fix {
            span: span.to_owned(),
            term: term.to_owned(),
        }
    }

    #[test]
    fn live_mishearings_sound_alike_and_unrelated_words_do_not() {
        assert!(sounds_alike("Ormis", "Hermes"));
        assert!(sounds_alike("below voice", "Willow Voice"));
        assert!(sounds_alike("10x", "Telnyx"));
        assert!(sounds_alike("Bapi", "Vapi"));
        assert!(!sounds_alike("elixir", "Telnyx"));
        assert!(!sounds_alike("anime", "Trakt"));
    }

    #[test]
    fn validate_keeps_only_gated_fixes() {
        let allowed = vec![String::from("Hermes"), String::from("Telnyx")];
        let text = "Ormis has been acting up, ask the elixir team";
        let accepted = validate(
            text,
            &[
                fix("Ormis", "hermes"),
                fix("elixir", "Telnyx"),
                fix("acting", "Kimi"),
                fix("Missing", "Hermes"),
                fix("has been acting up", "Hermes"),
            ],
            &allowed,
        );
        assert_eq!(accepted, vec![fix("Ormis", "Hermes")]);
    }

    #[test]
    fn apply_replaces_whole_words_only() {
        let fixes = vec![fix("Ormis", "Hermes")];
        assert_eq!(
            apply("Ormis broke, Ormisky did not", &fixes),
            "Hermes broke, Ormisky did not"
        );
    }

    #[test]
    fn parse_accepts_wrapped_json_and_rejects_noise() {
        assert_eq!(
            parse_fixes("Sure: {\"fixes\":[{\"span\":\"Ormis\",\"term\":\"Hermes\"}]}"),
            vec![fix("Ormis", "Hermes")]
        );
        assert!(parse_fixes("{\"fixes\":[]}").is_empty());
        assert!(parse_fixes("no json here").is_empty());
    }

    #[test]
    fn screen_terms_take_names_not_sentence_openers() {
        let common = |word: &str| matches!(word.to_ascii_lowercase().as_str(), "the" | "can");
        assert_eq!(
            screen_terms("The OpenChamber session. Can Hermes restart?", common),
            vec![String::from("OpenChamber"), String::from("Hermes")]
        );
    }
}
