import time
import uuid
from typing import Any, Dict, List, Optional, Set, Union

import pandas as pd

from mlflow.entities import Assessment as AssessmentEntity
from mlflow.entities import Evaluation as EvaluationEntity
from mlflow.entities import Metric
from mlflow.evaluation.evaluation import Assessment, Evaluation
from mlflow.evaluation.utils import (
    append_to_assessments_dataframe,
    compute_assessment_stats_by_source,
    dataframes_to_evaluations,
    evaluations_to_dataframes,
    read_assessments_dataframe,
    read_evaluations_dataframe,
    read_metrics_dataframe,
    verify_assessments_have_same_value_type,
)
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import (
    INTERNAL_ERROR,
    INVALID_PARAMETER_VALUE,
    RESOURCE_DOES_NOT_EXIST,
)
from mlflow.tracking.client import MlflowClient
from mlflow.tracking.fluent import _get_or_start_run


def log_evaluation(
    *,
    inputs: Dict[str, Any],
    outputs: Dict[str, Any],
    inputs_id: Optional[str] = None,
    request_id: Optional[str] = None,
    targets: Optional[Dict[str, Any]] = None,
    assessments: Optional[Union[List[Assessment], List[Dict[str, Any]]]] = None,
    metrics: Optional[Union[List[Metric], Dict[str, float]]] = None,
    run_id: Optional[str] = None,
) -> EvaluationEntity:
    """
    Logs an evaluation to an MLflow Run.

    Args:
      inputs (Dict[str, Any]): Input fields used by the model to compute outputs.
      outputs (Dict[str, Any]): Outputs computed by the model.
      inputs_id (Optional[str]): Unique identifier for the evaluation `inputs`. If not specified,
          a unique identifier is generated by hashing the inputs.
      request_id (Optional[str]): ID of an MLflow Trace corresponding to the inputs and outputs.
          If specified, displayed in the MLflow UI to help with root causing issues and identifying
          more granular areas for improvement when reviewing the evaluation and adding assessments.
      targets (Optional[Dict[str, Any]]): Targets (ground truths) corresponding to one or more of
          the evaluation `outputs`. Helps root cause issues when reviewing the evaluation and adding
          assessments.
      assessments (Optional[Union[List[Assessment], List[Dict[str, Any]]]]): Assessment of the
          evaluation, e.g., relevance of documents retrieved by a RAG model to a user input query,
          as assessed by an LLM Judge.
      metrics (Optional[Union[List[Metric], Dict[str, float]]]): Numerical metrics for the
          evaluation, e.g., "number of input tokens", "number of output tokens".
      run_id (Optional[str]): ID of the MLflow Run to log the evaluation. If unspecified, the
          current active run is used.

    Returns:
       EvaluationEntity: The logged Evaluation object.
    """
    if assessments and isinstance(assessments[0], dict):
        if not all(isinstance(assess, dict) for assess in assessments):
            raise MlflowException(
                "If `assessments` contains a dictionary, all elements must be dictionaries.",
                error_code=INVALID_PARAMETER_VALUE,
            )
        assessments = [Assessment.from_dictionary(assess) for assess in assessments]
    verify_assessments_have_same_value_type(assessments)

    if metrics and isinstance(metrics, dict):
        metrics = [
            Metric(key=k, value=v, timestamp=int(time.time() * 1000), step=0)
            for k, v in metrics.items()
        ]

    evaluation = Evaluation(
        inputs=inputs,
        outputs=outputs,
        inputs_id=inputs_id,
        request_id=request_id,
        targets=targets,
        assessments=assessments,
        metrics=metrics,
    )

    return log_evaluations(evaluations=[evaluation], run_id=run_id)[0]


def log_evaluations(
    *, evaluations: List[Evaluation], run_id: Optional[str] = None
) -> List[EvaluationEntity]:
    """
    Logs one or more evaluations to an MLflow Run.

    Args:
      evaluations (List[Evaluation]): List of one or more MLflow Evaluation objects.
      run_id (Optional[str]): ID of the MLflow Run to log the evaluation. If unspecified, the
          current active run is used.

    Returns:
      List[EvaluationEntity]: The logged Evaluation objects.
    """
    run_id = run_id if run_id is not None else _get_or_start_run().info.run_id
    client = MlflowClient()
    evaluation_entities = [
        evaluation._to_entity(run_id=run_id, evaluation_id=uuid.uuid4().hex)
        for evaluation in evaluations
    ]
    evaluations_df, metrics_df, assessments_df = evaluations_to_dataframes(evaluation_entities)
    client.log_table(
        run_id=run_id, data=evaluations_df, artifact_file="_evaluations.json", set_tag=False
    )
    client.log_table(run_id=run_id, data=metrics_df, artifact_file="_metrics.json", set_tag=False)
    client.log_table(
        run_id=run_id, data=assessments_df, artifact_file="_assessments.json", set_tag=False
    )

    _update_assessments_stats(
        run_id=run_id,
        assessments_df=assessments_df,
        assessment_names=assessments_df["name"].unique(),
    )
    return evaluation_entities


def log_evaluations_df(
    *,
    run_id: str,
    evaluations_df: pd.DataFrame,
    input_cols: List[str],
    output_cols: List[str],
    inputs_id_col: Optional[str] = None,
    target_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Logs one or more evaluations from a DataFrame to an MLflow Run.

    Args:
      run_id (str): ID of the MLflow Run to log the Evaluations.
      evaluations_df (pd.DataFrame): Pandas DataFrame containing the evaluations to log.
          Must contain the columns specified in `input_cols`, `output_cols`, and
          `target_cols`.
          Additionally, evaluation information will be read from the following optional columns,
          if specified (see documentation for the log_evaluations() API):
              - "inputs_id": Unique identifier for evaluation inputs.
              - "request_id": ID of an MLflow trace corresponding to the evaluation inputs and
                  outputs.
              - "metrics": Numerical evaluation metrics, represented as a list of MLflow Metric
                  objects or as a dictionary.
      input_cols (List[str]): Names of columns containing input fields for evaluation.
      output_cols (List[str]): Names of columns containing output fields for evaluation.
      inputs_id_col (Optional[str]): Name of the column containing unique identifiers for the
          inputs. If not specified, a unique identifier is generated by hashing the inputs.
      target_cols (Optional[List[str]]): Names of columns containing targets (ground truths) for
          evaluation.

    Returns:
      pd.DataFrame: The specified evaluations DataFrame, with an additional "evaluation_id" column
          containing the IDs of the logged evaluations.
    """
    # Extract columns for Evaluation objects
    eval_data = evaluations_df[input_cols + output_cols]
    target_data = evaluations_df[target_cols] if target_cols else None

    # Create a list of Evaluation objects
    evaluations = []
    for _, row in eval_data.iterrows():
        inputs = row[input_cols].to_dict()
        outputs = row[output_cols].to_dict()
        targets = row[target_cols].to_dict() if target_data is not None else None
        inputs_id = row[inputs_id_col] if inputs_id_col else None
        evaluations.append(
            Evaluation(
                inputs=inputs,
                outputs=outputs,
                inputs_id=inputs_id,
                targets=targets,
            )
        )

    # Log evaluations
    evaluation_entities = log_evaluations(evaluations=evaluations, run_id=run_id)

    # Add evaluation_id column to main DataFrame for the result
    evaluations_df["evaluation_id"] = [
        eval_entity.evaluation_id for eval_entity in evaluation_entities
    ]

    return evaluations_df


def log_assessments(
    *,
    evaluation_id: str,
    assessments: Union[Assessment, List[Assessment], Dict[str, Any], List[Dict[str, Any]]],
    run_id: Optional[str] = None,
):
    """
    Logs assessments to an existing Evaluation.

    Args:
        evaluation_id (str): The ID of the evaluation.
        assessments (Union[Assessment, List[Assessment], Dict[str, Any], List[Dict[str, Any]]]):
            An MLflow Assessment object, a dictionary representation of MLflow Assessment objects,
            or a list of these objects / dictionaries.
        run_id (Optional[str]): ID of the MLflow Run containing the evaluation to which to log the
            assessments. If unspecified, the current active run is used.
    """
    run_id = run_id if run_id is not None else _get_or_start_run().info.run_id
    # Fetch the evaluation from the run to verify that it exists
    get_evaluation(run_id=run_id, evaluation_id=evaluation_id)
    client = MlflowClient()

    if isinstance(assessments, dict):
        assessments = [Assessment.from_dictionary(assessments)]
    elif isinstance(assessments, list):
        if any(isinstance(assess, dict) for assess in assessments):
            if not all(isinstance(assess, dict) for assess in assessments):
                raise ValueError(
                    "If `assessments` contains a dictionary, all elements must be dictionaries."
                )
            assessments = [Assessment.from_dictionary(assess) for assess in assessments]
    else:
        assessments = [assessments]
    assessments = [assess._to_entity(evaluation_id=evaluation_id) for assess in assessments]

    assessments_file = client.download_artifacts(run_id=run_id, path="_assessments.json")
    assessments_df = read_assessments_dataframe(assessments_file)
    for assessment in assessments:
        assessments_df = _add_assessment_to_df(
            assessments_df=assessments_df, assessment=assessment, evaluation_id=evaluation_id
        )

    _update_assessments_stats(
        run_id=run_id,
        assessments_df=assessments_df,
        assessment_names={assess.name for assess in assessments},
    )

    with client._log_artifact_helper(run_id, "_assessments.json") as tmp_path:
        assessments_df.to_json(tmp_path, orient="split")


def _add_assessment_to_df(
    assessments_df: pd.DataFrame, assessment: AssessmentEntity, evaluation_id: str
) -> pd.DataFrame:
    """
    Adds or updates an assessment in the assessments DataFrame.

    Args:
        assessments_df (pd.DataFrame): The DataFrame containing existing assessments.
        assessment (Assessment): The new assessment to add or update.
        evaluation_id (str): The ID of the evaluation.

    Returns:
        pd.DataFrame: The updated DataFrame with the new or updated assessment.
    """
    # Get assessments with the same name and verify that the type is the same (boolean,
    # numeric, or string)
    existing_assessments_matching_name_df = assessments_df[
        (assessments_df["evaluation_id"] == evaluation_id)
        & (assessments_df["name"] == assessment.name)
    ]
    existing_assessments_matching_name = [
        AssessmentEntity.from_dictionary(assess)
        for assess in existing_assessments_matching_name_df.to_dict(orient="records")
    ]
    if existing_assessments_matching_name:
        existing_assessments_value_type = existing_assessments_matching_name[0].get_value_type()
        if not all(
            assessment.get_value_type() == existing_assessment.get_value_type()
            for existing_assessment in existing_assessments_matching_name
        ):
            raise MlflowException(
                f"Assessment with name '{assessment.name}' has value type "
                f"'{assessment.get_value_type()}' that does not match the value type "
                f"'{existing_assessments_value_type}' of existing assessments with the same name.",
                error_code=INVALID_PARAMETER_VALUE,
            )

    # Check if assessment with the same name and source already exists
    existing_assessment_index = assessments_df[
        (assessments_df["evaluation_id"] == evaluation_id)
        & (assessments_df["name"] == assessment.name)
        & (assessments_df["source"] == assessment.source.to_dictionary())
    ].index

    if not existing_assessment_index.empty:
        # Update existing assessment
        # TODO: Move this into a util function and refactor for schema maintenance
        assessment_dict = assessment.to_dictionary()
        assessment_dict["evaluation_id"] = evaluation_id
        assessments_df.loc[
            existing_assessment_index, assessment_dict.keys()
        ] = assessment_dict.values()
    else:
        # Append new assessment
        assessments_df = append_to_assessments_dataframe(assessments_df, [assessment])

    return assessments_df


def _update_assessments_stats(
    run_id: str, assessments_df: pd.DataFrame, assessment_names: Set[str]
):
    """
    Updates the specified MLflow Run by logging MLflow Metrics with statistics for the
    specified assessment names, aggregated by source.

    Args:
        run_id (str): ID of the MLflow Run to update.
        assessments_df (pd.DataFrame): DataFrame containing the assessments.
        assessment_names (Set[str]): Names of the assessments for which to update statistics.
    """
    client = MlflowClient()
    for assessment_name in assessment_names:
        assessment_stats_by_source = compute_assessment_stats_by_source(
            assessments_df=assessments_df, assessment_name=assessment_name
        )
        for stats in assessment_stats_by_source.values():
            client.log_batch(run_id=run_id, metrics=stats.to_metrics())


def get_evaluation(*, run_id: str, evaluation_id: str) -> EvaluationEntity:
    """
    Retrieves an Evaluation object from an MLflow Run.

    Args:
        run_id (str): ID of the MLflow Run containing the evaluation.
        evaluation_id (str): The ID of the evaluation.

    Returns:
        Evaluation: The Evaluation object.
    """

    def _contains_evaluation_artifacts(client: MlflowClient, run_id: str) -> bool:
        return (
            any(file.path == "_evaluations.json" for file in client.list_artifacts(run_id))
            and any(file.path == "_metrics.json" for file in client.list_artifacts(run_id))
            and any(file.path == "_assessments.json" for file in client.list_artifacts(run_id))
        )

    client = MlflowClient()
    if not _contains_evaluation_artifacts(client, run_id):
        raise MlflowException(
            "The specified run does not contain any evaluations. "
            "Please log evaluations to the run before retrieving them.",
            error_code=RESOURCE_DOES_NOT_EXIST,
        )

    evaluations_file = client.download_artifacts(run_id=run_id, path="_evaluations.json")
    evaluations_df = read_evaluations_dataframe(evaluations_file)

    evaluation_row = evaluations_df[evaluations_df["evaluation_id"] == evaluation_id]
    if evaluation_row.empty:
        raise MlflowException(
            f"The specified evaluation ID '{evaluation_id}' does not exist in the run '{run_id}'.",
            error_code=RESOURCE_DOES_NOT_EXIST,
        )

    # Extract metrics and assessments
    metrics_file = client.download_artifacts(run_id=run_id, path="_metrics.json")
    metrics_df = read_metrics_dataframe(metrics_file)

    assessments_file = client.download_artifacts(run_id=run_id, path="_assessments.json")
    assessments_df = read_assessments_dataframe(assessments_file)

    evaluations: List[Evaluation] = dataframes_to_evaluations(
        evaluations_df=evaluation_row, metrics_df=metrics_df, assessments_df=assessments_df
    )
    if len(evaluations) != 1:
        raise MlflowException(
            f"Expected to find a single evaluation with ID '{evaluation_id}', but found "
            f"{len(evaluations)} evaluations.",
            error_code=INTERNAL_ERROR,
        )

    return evaluations[0]
