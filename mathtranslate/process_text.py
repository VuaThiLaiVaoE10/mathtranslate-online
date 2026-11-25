char_limit = 2000


def is_connected(line_above, line_below):
    if len(line_above) > 0 and len(line_below) > 0:
        if line_above[-1] != '.' and line_below[0].islower():
            return True
    return False


def connect_paragraphs(text):
    text_split = text.split('\n')
    i = 0
    while i < len(text_split) - 1:
        line_above = text_split[i]
        line_below = text_split[i + 1]
        if is_connected(line_above, line_below):
            text_split[i] = text_split[i] + text_split[i + 1]
            del text_split[i + 1]
        else:
            i += 1
    return '\n'.join(text_split)


def get_first_word(line):
    words = line.split(' ')
    for word in words:
        if len(word) > 0:
            return word
    return ''


def argmax(array):
    return array.index(max(array))


def _split_long_paragraph(paragraph):
    """Split a single paragraph until every resulting line is strictly shorter than char_limit."""
    if len(paragraph) < char_limit:
        return [paragraph]

    lines = paragraph.split('.')
    first_words = [get_first_word(line) for line in lines]
    first_length = [len(word) if (len(word) > 0 and word[0].isupper()) else 0 for word in first_words]
    first_length[0] = 0
    position = argmax(first_length)

    if position > 0:
        left = '.'.join(lines[0:position]) + '.'
        right = '.'.join(lines[position:])
        if 0 < len(left) < len(paragraph) and 0 < len(right) < len(paragraph):
            return _split_long_paragraph(left) + _split_long_paragraph(right)

    # Safe fallback when no uppercase sentence boundary exists (or split makes no progress)
    max_chunk = max(1, min(char_limit - 1, char_limit - 20))
    for sep, keep_sep in [('. ', True), (', ', True), (' ', False)]:
        idx = paragraph.rfind(sep, 1, max_chunk)
        if idx > 0:
            left = paragraph[:idx + (1 if keep_sep else 0)]
            right = paragraph[idx + len(sep):]
            return [left] + _split_long_paragraph(right)

    return [paragraph[:max_chunk]] + _split_long_paragraph(paragraph[max_chunk:])


def split_too_long_paragraphs(text):
    text_split = []
    for paragraph in text.split('\n'):
        if len(paragraph) >= char_limit:
            text_split.extend(_split_long_paragraph(paragraph))
        else:
            text_split.append(paragraph)
    return '\n'.join(text_split)


def is_title(line_above, line_below):
    if len(line_above) > 0 and len(line_below) > 0:
        if line_above[-1] != '.' and (not line_above[0].islower()) and line_below[0].isupper():
            return True
    return False


def split_titles(text):
    text_split = text.split('\n')
    i = 0
    while i < len(text_split) - 1:
        line_above = text_split[i]
        line_below = text_split[i + 1]
        if is_title(line_above, line_below):
            text_split[i] = '\n\n' + text_split[i] + '\n\n'
        i += 1
    return '\n'.join(text_split)
