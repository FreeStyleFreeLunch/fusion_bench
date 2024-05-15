from datasets import load_dataset
from omegaconf import DictConfig, open_dict


def load_dataset_from_config(dataset_config: DictConfig):
    """
    Load the dataset from the configuration.
    """
    assert hasattr(dataset_config, "type"), "Dataset type not specified"
    if dataset_config.type == "huggingface_image_classification":
        if not hasattr(dataset_config, "path"):
            with open_dict(dataset_config):
                dataset_config.path = dataset_config.name

        dataset = load_dataset(
            dataset_config.path,
            **(dataset_config.kwargs if hasattr(dataset_config, "kwargs") else {}),
        )
        if hasattr(dataset_config, "split"):
            dataset = dataset[dataset_config.split]
        return dataset
    else:
        raise ValueError(f"Unknown dataset type: {dataset_config.type}")