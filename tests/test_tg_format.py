from loop_orchestrator.clients.tg_format import md_to_telegram_html


def test_headers_become_bold():
    assert md_to_telegram_html("## Резюме") == "<b>Резюме</b>"
    assert md_to_telegram_html("### Что сделано") == "<b>Что сделано</b>"


def test_inline_styles():
    assert md_to_telegram_html("это **важно** и *тонко*") == "это <b>важно</b> и <i>тонко</i>"
    assert md_to_telegram_html("файл `core.py` готов") == "файл <code>core.py</code> готов"
    assert md_to_telegram_html("~~зачёркнуто~~") == "<s>зачёркнуто</s>"


def test_fenced_code_block_escaped_verbatim():
    got = md_to_telegram_html("```python\nif a < b:\n    pass\n```")
    assert got == "<pre>if a &lt; b:\n    pass</pre>"
    # markdown inside code must NOT be styled
    assert md_to_telegram_html("`**not bold**`") == "<code>**not bold**</code>"


def test_bullets_and_links():
    assert md_to_telegram_html("- один\n- два") == "• один\n• два"
    got = md_to_telegram_html("[спека](https://example.com/a?b=1)")
    assert got == '<a href="https://example.com/a?b=1">спека</a>'


def test_raw_html_is_escaped():
    assert md_to_telegram_html("x <b>y</b> & z") == "x &lt;b&gt;y&lt;/b&gt; &amp; z"


def test_table_becomes_aligned_pre():
    md = ("| Commit | Content |\n"
          "|---|---|\n"
          "| a1 | Task one |\n"
          "| b2 | Task two |")
    assert md_to_telegram_html(md) == (
        "<pre>Commit  Content\n"
        "----------------\n"
        "a1      Task one\n"
        "b2      Task two</pre>")


def test_table_cells_lose_inline_markdown_and_escape():
    md = ("| File | Note |\n"
          "|---|---|\n"
          "| `a.py` | **bold** [x](https://e.com) & <i> |")
    got = md_to_telegram_html(md)
    assert got.startswith("<pre>") and got.endswith("</pre>")
    assert "a.py" in got and "bold x" in got
    assert "`" not in got and "**" not in got and "https://e.com" not in got
    assert "&amp;" in got and "&lt;i&gt;" in got


def test_table_inside_summary_and_lone_pipe_line_kept():
    md = "## Итог\n\n| K | V |\n|---|---|\n| a | b |\n\nстрока с | пайпом"
    got = md_to_telegram_html(md)
    assert "<b>Итог</b>" in got
    assert "<pre>K  V\n----\na  b</pre>" in got
    assert "строка с | пайпом" in got


def test_mixed_summary():
    md = "## Итог\n\n**Тесты:** `10 passed`\n- пункт *раз*"
    assert md_to_telegram_html(md) == (
        "<b>Итог</b>\n\n<b>Тесты:</b> <code>10 passed</code>\n• пункт <i>раз</i>")
