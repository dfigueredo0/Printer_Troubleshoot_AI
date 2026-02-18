# MIT License

import json
from .metrics import compute_accuracy


def main():
    result = {"accuracy": compute_accuracy([1, 1], [1, 0])}
    with open("reports/eval.json", "w") as f:
        json.dump(result, f)
    print("Evaluation complete.")


if __name__ == "__main__":
    main()
