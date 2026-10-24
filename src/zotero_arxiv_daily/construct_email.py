from .protocol import Paper
import math


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em; /* 调整星星大小 */
      line-height: 1; /* 确保垂直对齐 */
      display: inline-flex;
      align-items: center; /* 保持对齐 */
    }
    .half-star {
      display: inline-block;
      width: 0.5em; /* 半颗星的宽度 */
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>

<div>
    __CONTENT__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No Papers Today. Take a Rest!
    </td>
  </tr>
  </table>
  """
  return block_template

def get_block_html(title:str, authors:str, rate:str, tldr:str, pdf_url:str, affiliations:str=None, summary_label:str="TLDR", abstract_zh:str=None, abstract_zh_label:str="摘要翻译"):
    # 没有 TLDR 时整行不渲染，避免邮件里出现字面量 "TLDR: None"
    summary_row = ''
    if tldr:
        summary_row = f"""
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>{summary_label}:</strong> {tldr}
        </td>
    </tr>
"""
    # 中文摘要翻译：作为独立一行跟在 TLDR 后面，同样允许整体缺失
    abstract_zh_row = ''
    if abstract_zh:
        abstract_zh_row = f"""
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0; background-color: #f0f7ff; border-left: 3px solid #337ab7; padding-left: 10px;">
            <strong>{abstract_zh_label}:</strong> {abstract_zh}
        </td>
    </tr>
"""
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
            {authors}
            <br>
            <i>{affiliations}</i>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>Relevance:</strong> {rate}
        </td>
    </tr>
{summary_row}{abstract_zh_row}
    <tr>
        <td style="padding: 8px 0;">
            <a href="{pdf_url}" style="display: inline-block; text-decoration: none; font-size: 14px; font-weight: bold; color: #fff; background-color: #d9534f; padding: 8px 16px; border-radius: 4px;">PDF</a>
        </td>
    </tr>
</table>
"""
    return block_template.format(title=title, authors=authors, rate=rate, abstract_zh_row=abstract_zh_row, summary_row=summary_row, pdf_url=pdf_url, affiliations=affiliations)

def get_stars(score:float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    else:
        interval = (high-low) / 10
        star_num = math.ceil((score-low) / interval)
        full_star_num = int(star_num/2)
        half_star_num = star_num - full_star_num * 2
        return '<div class="star-wrapper">'+full_star * full_star_num + half_star * half_star_num + '</div>'


def _shorten(text: str, limit: int = 600) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + '...'


def render_email(papers:list[Paper]) -> str:
    parts = []
    if len(papers) == 0 :
        return framework.replace('__CONTENT__', get_empty_html())
    
    for p in papers:
        #rate = get_stars(p.score)
        rate = round(p.score, 1) if p.score is not None else 'Unknown'
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ', '.join(author_list)
        else:
            authors = ', '.join(author_list[:3] + ['...'] + author_list[-2:])
        if p.affiliations is not None:
            affiliations = p.affiliations[:5]
            affiliations = ', '.join(affiliations)
            if len(p.affiliations) > 5:
                affiliations += ', ...'
        else:
            affiliations = 'Unknown Affiliation'
        # 有 TLDR 用 TLDR；没配 LLM 时退回摘要，保证邮件里仍有可读的一句话
        if p.tldr:
            summary_label, summary = 'TLDR', p.tldr
        elif p.abstract:
            summary_label, summary = 'Abstract', _shorten(p.abstract)
        else:
            summary_label, summary = 'TLDR', None
        # 中文摘要翻译：由 protocol.Paper.generate_abstract_zh 生成，失败时为 None
        abstract_zh = getattr(p, "abstract_zh", None)
        if abstract_zh:
            abstract_zh_label = "中文摘要"
        else:
            abstract_zh_label = "摘要翻译"
        parts.append(get_block_html(p.title, authors, rate, summary, p.pdf_url, affiliations, summary_label, abstract_zh, abstract_zh_label))

    content = '<br>' + '</br><br>'.join(parts) + '</br>'
    return framework.replace('__CONTENT__', content)
