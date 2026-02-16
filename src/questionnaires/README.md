# Questionnaires App

The questionnaires app provides a comprehensive system for creating, managing, and evaluating questionnaires with AI-powered evaluation capabilities and prompt injection protection.

## Overview

This Django app implements a flexible questionnaire system that supports:
- **Multiple question types** (multiple choice, free text, file upload)
- **AI-powered evaluation** with pluggable LLM backends via Instructor
- **Prompt injection detection** using the Sentinel model
- **Manual, automatic, and hybrid evaluation modes**
- **Scoring system** with weighted questions and fatal question handling
- **Section-based organization** with flexible shuffling options

## Core Components

### Models (`models.py`)

#### Questionnaire Hierarchy
```
Questionnaire
├── QuestionnaireSection (optional)
│   ├── MultipleChoiceQuestion
│   │   └── MultipleChoiceOption
│   ├── FreeTextQuestion
│   └── FileUploadQuestion
└── Direct Questions (no section)
    ├── MultipleChoiceQuestion
    ├── FreeTextQuestion
    └── FileUploadQuestion
```

#### Key Model Features
- **Questionnaire**: Configurable evaluation modes, scoring thresholds, LLM backends
- **Questions**: Weighted scoring, mandatory/fatal flags, ordering/shuffling
- **Submissions**: Draft/ready states, user tracking, submission timestamps
- **Evaluations**: Score tracking, status management, audit trails

### Service Layer (`service.py`, `evaluator.py`)

#### QuestionnaireService
- **CRUD Operations**: Create/update questionnaires, sections, questions
- **Submission Handling**: Process user submissions with validation
- **Transaction Safety**: Atomic operations for data consistency

#### SubmissionEvaluator
- **Automated Scoring**: Calculates scores for multiple choice and free text questions
- **LLM Integration**: Batch evaluation of free text answers
- **Business Rules**: Handles fatal questions, mandatory validation, scoring thresholds

### LLM System (`llms/`)

#### Architecture
```
FreeTextEvaluator (Protocol)
├── MockEvaluator (testing)
└── BaseLLMEvaluator (defensive prompting)
    └── SanitizingLLMEvaluator (content filtering — default)
        └── SentinelLLMEvaluator (ML prompt injection detection)
```

The system uses **Instructor** for vendor-agnostic LLM access with validated structured outputs. Any provider supported by Instructor can be used (OpenAI, Anthropic, Ollama, Google Gemini, Mistral, etc.).

#### Evaluation Backends

**MockEvaluator**
- Simple keyword-based evaluation (contains "good" = pass)
- Used for testing and development

**SanitizingLLMEvaluator** (default)
- Inherits from BaseLLMEvaluator (defensive prompting with XML tag isolation)
- Strips HTML-like tags from user input before evaluation
- Prevents tag-based injection attempts

**SentinelLLMEvaluator**
- Inherits from SanitizingLLMEvaluator (gets both sanitization + defensive prompting)
- Uses machine learning model to detect prompt injection
- **Complete failure policy**: Any detected injection causes total evaluation failure
- In-memory model caching for performance

### Prompt Injection Protection

#### Sentinel Model Integration
The app uses the `qualifire/prompt-injection-sentinel` model for advanced prompt injection detection:

**Setup**
```bash
# Download the model locally
python manage.py download_sentinel_model
```

**Detection Process**
1. Load model once into memory for reuse
2. Check all answer texts for injection
3. Return "benign" or "jailbreak" classification
4. **Zero tolerance**: Any "jailbreak" detection = complete evaluation failure

**Security Policy**
- Prompt injection attempts result in immediate failure
- No partial evaluation - entire submission marked as failed
- Protects against sophisticated injection attacks

## API Integration

### Evaluation Modes
- **AUTOMATIC**: AI evaluation with immediate pass/fail
- **MANUAL**: Human review required for all submissions
- **HYBRID**: AI evaluation + mandatory human review

### Scoring System
- **Weighted Questions**: Positive/negative point values per question
- **Fatal Questions**: Single wrong answer = automatic failure
- **Mandatory Questions**: Unanswered mandatory = automatic failure
- **Threshold-based**: Configurable minimum score for passing

## Usage Examples

### Creating a Questionnaire
```python
from questionnaires.service import QuestionnaireService
from questionnaires.schema import QuestionnaireCreateSchema

# Create questionnaire with AI evaluation
questionnaire = QuestionnaireService.create_questionnaire(
    QuestionnaireCreateSchema(
        name="Security Awareness Quiz",
        evaluation_mode="automatic",
        llm_backend="questionnaires.llms.SentinelLLMEvaluator",
        llm_guidelines="Evaluate based on cybersecurity best practices...",
        min_score=80.0,
        # ... questions and sections
    )
)
```

### Evaluating Submissions
```python
from questionnaires.evaluator import SubmissionEvaluator

# Automatic evaluation with prompt injection protection
evaluator = SubmissionEvaluator(submission)
evaluation = evaluator.evaluate()
print(f"Score: {evaluation.score}, Status: {evaluation.status}")
```

### Using Different LLM Backends
```python
# Mock for testing
questionnaire.llm_backend = "questionnaires.llms.MockEvaluator"

# Prompt injection protection (sanitization + ML detection)
questionnaire.llm_backend = "questionnaires.llms.SentinelLLMEvaluator"

# Content sanitization (default)
questionnaire.llm_backend = "questionnaires.llms.SanitizingLLMEvaluator"
```

## Configuration

### LLM Settings

The LLM system is configured via environment variables:

```bash
# Model identifier including provider prefix: "provider/model-name"
LLM_DEFAULT_MODEL="ollama/llama3.1:8b"  # Local dev (default)
# LLM_DEFAULT_MODEL="openai/gpt-4o-mini"  # Production
LLM_MAX_RETRIES=3

# API key — not needed for local providers like Ollama
# LLM_API_KEY=sk-...

# Override the provider's default base URL (typically not needed)
# LLM_BASE_URL=

# Instructor mode for structured output extraction.
# Valid values: JSON, TOOLS, JSON_SCHEMA, MD_JSON (see instructor.Mode).
# Empty string = let Instructor auto-detect based on the provider.
# Default: JSON (safest for small Ollama models — see note below).
# LLM_INSTRUCTOR_MODE=JSON
```

#### Instructor Mode: JSON vs TOOLS

Instructor supports two main approaches for extracting structured output from LLMs:

- **JSON** (`LLM_INSTRUCTOR_MODE=JSON`): The LLM returns JSON in the message content, validated by Instructor against the Pydantic schema. Works with all providers and models.
- **TOOLS** (`LLM_INSTRUCTOR_MODE=TOOLS`): Uses the provider's native function/tool-calling API. More reliable with large models that support it (e.g. GPT-4o, Claude).

**Why JSON is the default:** Instructor auto-detects the mode based on the provider and model name. However, some smaller models (e.g. `ollama/llama3.1:8b`) are listed as "tool capable" in Instructor's registry but don't handle TOOLS mode reliably — they fail with `Instructor does not support multiple tool calls`. Setting the mode to JSON explicitly avoids this issue.

**When to use TOOLS:** If you're using a provider/model that fully supports function calling (OpenAI GPT-4o, Anthropic Claude, etc.), you can set `LLM_INSTRUCTOR_MODE=TOOLS` or leave it empty for auto-detection.

See `revel/settings/llm.py` for Django settings and https://python.useinstructor.com/integrations/ for all supported providers.

### LLM Backend Selection
Configure in the Questionnaire model:
- `MockEvaluator`: Development and testing
- `SanitizingLLMEvaluator`: Content filtering + defensive prompting (default)
- `SentinelLLMEvaluator`: ML-based prompt injection detection + sanitization

## Management Commands

### Download Sentinel Model
```bash
python manage.py download_sentinel_model
```
Downloads the prompt injection detection model to `questionnaires/llms/sentinel/`

## Security Features

### Prompt Injection Protection
- **Defensive Prompting**: XML tag isolation with explicit instructions to ignore injected content
- **Content Sanitization**: Strips potentially harmful markup before LLM evaluation
- **Sentinel Model**: Machine learning classification of malicious inputs
- **Zero Tolerance Policy**: Any injection attempt = complete failure

### Data Validation
- **Schema Validation**: Pydantic models ensure data integrity
- **Cross-reference Checks**: Prevents mixing data across questionnaires
- **Constraint Enforcement**: Database-level uniqueness and referential integrity
- **Transaction Safety**: Atomic operations prevent partial state corruption

## Testing Strategy

### Comprehensive Test Coverage
- **Unit Tests**: Individual model methods and validators
- **Integration Tests**: Full submission and evaluation workflows
- **Edge Case Testing**: Boundary conditions, empty data, invalid inputs
- **Security Testing**: Prompt injection attempts, content sanitization
- **Error Handling**: Exception raising and transaction rollback

### Test Fixtures
- Reusable questionnaire structures
- User authentication setup
- Mock LLM evaluation responses
- Various submission states and scenarios

## Performance Considerations

### Optimization Features
- **Batch LLM Evaluation**: Process multiple questions simultaneously
- **Model Caching**: Load Sentinel model once, reuse across requests
- **Query Optimization**: Prefetch related objects to minimize database hits
- **Lazy Loading**: Models loaded only when needed

### Async Processing
- **Celery Integration**: Background evaluation tasks
- **Notification System**: Email alerts for evaluation results
- **Task Queuing**: Handle high-volume submission processing

## Administrative Interface

### Django Admin Integration
- **Rich Admin Views**: Comprehensive questionnaire management
- **Inline Editing**: Questions and options within questionnaires
- **Evaluation Workflow**: Review pending evaluations, audit trails
- **Data Visualization**: Score displays, status indicators, user links
- **Bulk Operations**: Efficient management of large questionnaire datasets

## Error Handling

### Custom Exceptions
- `CrossQuestionnaireSubmissionError`: Mixed questionnaire data
- `MissingMandatoryAnswerError`: Unanswered required questions
- `SectionIntegrityError`: Invalid section references
- `QuestionIntegrityError`: Invalid question references
- `PromptInjectionDetectedError`: ML-detected injection attempts

### Validation Rules
- Mandatory question enforcement
- Unique submission constraints
- Cross-questionnaire data prevention
- Single/multiple answer consistency
- Score boundary validation (0-100)

## Future Extensibility

### Pluggable Architecture
- **LLM Backend Interface**: Easy addition of new evaluation providers
- **Question Types**: Extensible question type system
- **Evaluation Modes**: Configurable evaluation workflows
- **Scoring Algorithms**: Customizable scoring and weighting systems

This questionnaires app provides a robust, secure, and extensible foundation for AI-powered questionnaire evaluation with comprehensive prompt injection protection.
