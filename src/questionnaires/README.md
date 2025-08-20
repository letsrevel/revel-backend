# Questionnaires App

The questionnaires app provides a comprehensive system for creating, managing, and evaluating questionnaires with AI-powered evaluation capabilities and prompt injection protection.

## Overview

This Django app implements a flexible questionnaire system that supports:
- **Multiple question types** (multiple choice, free text)
- **AI-powered evaluation** with pluggable LLM backends
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
│   └── FreeTextQuestion
└── Direct Questions (no section)
    ├── MultipleChoiceQuestion
    └── FreeTextQuestion
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
├── VulnerableChatGPTEvaluator (demo)
├── BetterChatGPTEvaluator (secured)
├── SanitizingChatGPTEvaluator (content filtering)
└── SentinelChatGPTEvaluator (prompt injection detection)
```

#### Evaluation Backends

**MockEvaluator**
- Simple keyword-based evaluation (contains "good" = pass)
- Used for testing and development

**VulnerableChatGPTEvaluator**
- Basic ChatGPT integration
- Vulnerable to prompt injection attacks (for demonstration)

**BetterChatGPTEvaluator**
- Enhanced prompt structure with XML tags
- Defensive instructions against prompt injection
- Uses structured templates for consistent evaluation

**SanitizingChatGPTEvaluator**
- Inherits from BetterChatGPTEvaluator
- Strips HTML-like tags from user input before evaluation
- Prevents tag-based injection attempts

**SentinelChatGPTEvaluator** ⚡ **New**
- Inherits from BetterChatGPTEvaluator
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
2. Check all answer texts and guidelines for injection
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
        llm_backend="questionnaires.llms.SentinelChatGPTEvaluator",
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

# Prompt injection protection
questionnaire.llm_backend = "questionnaires.llms.SentinelChatGPTEvaluator"

# Content sanitization
questionnaire.llm_backend = "questionnaires.llms.SanitizingChatGPTEvaluator"
```

## Configuration

### Required Settings
```python
# settings.py
OPENAI_API_KEY = "your-openai-api-key"
HUGGING_FACE_HUB_TOKEN = "your-hf-token"  # For downloading Sentinel model
```

### LLM Backend Selection
Configure in the Questionnaire model:
- `MockEvaluator`: Development and testing
- `VulnerableChatGPTEvaluator`: Demonstration of vulnerabilities
- `BetterChatGPTEvaluator`: Improved prompt injection resistance
- `SanitizingChatGPTEvaluator`: Content filtering approach
- `SentinelChatGPTEvaluator`: ML-based prompt injection detection

## Management Commands

### Download Sentinel Model
```bash
python manage.py download_sentinel_model
```
Downloads the prompt injection detection model to `questionnaires/llms/sentinel/`

## Security Features

### Prompt Injection Protection
- **Multiple Defense Layers**: Template-based, sanitization, and ML detection
- **Sentinel Model**: Machine learning classification of malicious inputs
- **Zero Tolerance Policy**: Any injection attempt = complete failure
- **Content Sanitization**: Strips potentially harmful markup

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