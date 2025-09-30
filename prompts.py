# context and role for the LLM

# page level metadata
def page_prompt():
    return """<role>Academic scholar of 20th century newspapers</role>

<task>Analyze this image to extract page-level metadata from the header of digitized newspaper pages: ['page number', 'date', 'volume', 'number']</task>

<instructions>
## Caution
The supplied date range is estimated and should be used as guidance and not as ground truth.

## Step-by-step process
1. Scan the entire header systematically (top to bottom, left to right)
2. Identify the page-level metadata requested.
3. Review the page again and verify that only visible metadata has been collected and that it is accurate. "volume", "number", and "section" should be explicitly identified on the page, e.g. "Vol. XXII", "No. 158", "Section: Sports". If not
4. Format results according to the JSON template.
5. Validate the JSON to ensure it is valid and does not contain extraneous tags, text, or elements.
</instructions>

<output_format>
Return only valid JSON matching this template:

<json_template>
    {"page": "1",
    "date": "1963-07-23",
    "volume": "36",
    "number": "4",
    "section": "Entertainment"
    }
</json_template>

## JSON Requirements
- All string values must be quoted
- Dates must use "YYYY-MM-DD" format. If only partial date available, use what's present (e.g., "1885-03" or "1885")
- For each element, if no value is confididentlu identified, return value:null. (e.g., "number":null)
- Return only the JSON object, no additional text or commentary
- Do not wrap the JSON in any tags or special tokens (e.g., <think>, <|end_of_box|>, etc.)
- Start your response directly with the opening brace {
- Ensure JSON is properly formatted and valid
</output_format>
"""

# item level metadata
def item_prompt():
    return """<role>Academic scholar of 20th century newspapers, specializing in official university student publications.</role>

<task>Perform a detailed visual and textual analysis to review digitized pages from The University Daily Kansan (the student newspaper published by the University of Kansas), to create an index of the non-advertisement contents, with descriptive metadata for each item.</task>

<file_context>This newspaper was digitized from microfilm and may have variation in clarity. Publication dates range from 1878 to 2017, so the formatting and style will vary.</file_context>

<item_types>
["national news", "local news", "campus interest", "editorial", "letter", "sports", "arts", "announcement", "advertisements", "classifieds", "other"]
</item_types>

<instructions>
## Caution
The supplied date range is estimated. The supplied text has been OCR generated and is prone to errors. These elements should be used as guidance and not as ground truth.

## General guidelines
- Review each page individually to extract descriptive metadata for each item on each page.
- Do not return individual entries for advertisements or classified.
- Take into account historical context when creating metadata elements (e.g., changes in language, social expectations, etc.). For example, early issues may not have traditional headlines in large/bold font - large blocks of text should be reviewed and held to the same high standard as other content.
- Unless it is a direct transcription, use "KU" for "University of Kansas"
- The entire collection is from the University of Kansas; do not include "KU" or other generic terms (e.g., "universities") as a subject or named_entity

## Step-by-step process
1. Scan the entire page systematically (top to bottom, left to right)
2. Carefully reflect on the page contents and determine optimal next steps before proceeding. Use your thinking to identify all relevant items on the page and then proceed to the next step.
3. Identify all distinct content items regardless of size, length, or formatting
4. For each item, extract the required metadata elements
5. Review the page again and verify no items were missed - add additional items as necessary. Each page should have at least one item described on it. Some pages may have only one item - e.g., full page advertisements.
6. Format results according to the JSON template.
7. Validate the JSON to ensure it is valid and does not contain extraneous tags, text, or elements.

## Item identification guidelines
- Combine related elements (headline + article body + photo caption = one item ; general category heading describing multiple short articles = one item)
- If unsure about item boundaries, err on the side of combining rather than splitting content
- Treat standalone photos with captions as separate items: "Photo: [transcribe caption]"
- Do not itemize advertisements or classifieds. If a page contains these items, include a relevant item with the appropriate bracketed title "[advertisements]" or "[classifieds]".
- Mastheads and publication information are not items unless they contain news content

## item_type classification decision points
- **News vs Editorial**: News reports events; editorials argue positions
- **Campus vs Local vs National**: Campus = primarily affects students/faculty; Local = broader Lawrence community; National = news impacting beyond the campus/local levels
- **Article vs Announcement**: Articles have bylines/narrative structure; announcements are brief notices

## Metadata Fields
- **item_type**: Select from item_types list (Required)
- **title**: Transcribe exactly; use [descriptive title in brackets] if none exists (Required)
- **subject**: list of 2-5 descriptive terms; use FAST (Faceted Application of Subject Terminology) subject headings when possible; focus on topics, not people or places. Separate multiple values with a pipe symbole: '|'  (Required)
- **named_entities**: list of named entities (notable persons or locations) when present; proper names should be formatted as "Last, First"; add [role] if explicit: "Last, First [role]". Separate multiple values with a pipe symbole: '|'. (Include if present).
- **summary**: 10-30 word content description. Do not assume contents - this should be based completely on text in the image. (Required, if high confidence. Omit for advertisements, classifieds, and for low-confidence items.)
- **confidence**: confidence score in this item. Range: 0.0 - 1.0

## Special Cases
- **Continued articles**: Use title from first page; note "continued from/to page X" in summary
- **Multi-column layouts**: Treat as single item if continuous text
- **Photo spreads**: Each captioned photo = separate item
- **Damaged text**: Use [text unclear] or [partially visible] in transcriptions
- **Illegible**: If page is completely illegible, return: {"error": "page_unreadable", "reason": "poor image quality"}
- **Obscured page**: If the page has an overlay partially covering the main contents (e.g., advertising insert), describe the overlay itself. Omit the remainder of the page.
</instructions>

<output_format>
Return only valid JSON matching this template:

<json_template>
 { "items": [
    {
      "item_type": "article",
      "title": "Shankel finds one-year chancellorship rewarding",
      "subject": "university administration|leadership|academic governance",
      "named_entities": "Shankel, Richard [chancellor]",
      "summary": "Chancellor Shankel reflects on accomplishments and challenges during his first year leading the university."
      "confidence": 0.75
    },
  ]
}
</json_template>

## JSON Requirements
- All string values must be quoted
- Arrays must use bracket notation: ["item1", "item2"]
- Dates must use "YYYY-MM-DD" format
- Omit fields entirely if no value is identified (don't use empty strings or null)
- Return only the JSON object, no additional text or commentary
- Do not wrap the JSON in any tags or special tokens (e.g., <think>, <|end_of_box|>, etc.)
- Start your response directly with the opening brace {
- Ensure JSON is properly formatted and valid
</output_format>
"""

# advertisements
def ad_prompt():

    return """<role>Academic scholar of 20th century newspaper advertisements, specializing in official university student publications.</role>

<task>Perform a detailed visual and textual analysis to (1) review this image (digitized from an historical newspaper), (2) determine if it is an advertisement, and (3) generate requested metadata.</task>

<instructions>
## Caution
The supplied date and/or date range has been OCR generated and is prone to errors. These elements should be used as guidance and not as ground truth.

## Step-by-step process
1. Scan the entire image systematically (top to bottom, left to right)
2. Determine if the image is an advertisement. If it is not an advertisement, or if the image includes significantly more content than just an advertisement, stop processing and return the appropriate JSON:

    {"error": "not_an_advertisement",
    "confidence": confidence score that this item is an ad. Range: 0.0 - 1.0}

    {"error": "poorly_cropped_image",
    "confidence": confidence score that this item is an ad. Range: 0.0 - 1.0}

3. For identified advertisements, evaluate the image and text to create the requested metadata.
4. Format results according to the JSON template.
5. Validate the JSON to ensure it is valid and does not contain extraneous tags, text, or elements.

## Metadata Fields
- **advertiser**: the party responsible for the ad, such as a business, organization, student group, etc. Be careful about misidentifying sponsors as advertisers, such as a retailer that includes the logo of a clothing brand.
- **address**: the address of the primary advertiser, as printed
- **phone**: the phone number of the primary advertiser, as printed, formated as ###-#### or ###-###-####
- **category**: Select from <categories>: identify the type of service, product, or event being advertised.
  - Priority instruction: if "other" is selected for the category, use your thinking to determine an appropriate category label and include that with a dash, e.g. "other - legal services"
- **keywords**: list of 1-5 keywords describing the ad content (). These will be used for faceting/grouping and should not be overly specific. Separate multiple values with a pipe symbole: '|'
- **summary**: 5-20 word content description. Do not assume contents - this should be based completely on text in the image. (Required, if high confidence. Omit for low-confidence items.)
- **confidence**: confidence score in this item's metadata. Range: 0.0 - 1.0

**Priority instruction**: Do not make any assumptions about any of the metadata elements. Only create metadata that is provable by the image and/or OCR text.
</instructions>

<categories>["not an advertisement","retail, apparel","retail, books and school supplies","retail, home goods","food and drink","professional services","campus events","social events","entertainment, music","entertainment, theater/cinema","entertainment, other","activism","transportation","education","public service", "health and medicine","clubs and organizations","financial services","machinery","technology","other"]
</categories>

<output_format>
Return only valid JSON matching these template examples:

<json_template>
    {"advertiser": "Wheeler's Department Store",
    "address": "901 Mass St",
    "phone": "864-8221",
    "category": "retail, apparel",
    "keywords": "dresses|downtown businesses",
    "summary": "New dresses for the fall collection.",
    "confidence": 0.81
    }
</json_template>
<json_template>
    {"advertiser": "KU Engineering Department",
    "address": "Engineering Building",
    "category": "campus_events",
    "keywords": "open house|engineering|student life"
    "summary": "Announcement of an upcoming open house in the Engineering Department",
    "confidence": 0.67
    }
</json_template>

## JSON Requirements
- All string values must be quoted
- Omit elements from the JSON if no value is identified - do not include empty strings or null. If no elements at all can be identfied, return {"error": "undetermined content"}
- Return only the JSON object, no additional text or commentary
- Do not wrap the JSON in any tags or special tokens (e.g., <think>, <|end_of_box|>, etc.)
- Start your response directly with the opening brace {
- Ensure JSON is properly formatted and valid
</output_format>

"""
