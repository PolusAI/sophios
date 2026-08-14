import argparse
import random

parser = argparse.ArgumentParser()
parser.add_argument('--input_file', type=str)
parser.add_argument('--num_of_samples', type=int)
parser.add_argument('--random_seed', type=int)
parser.add_argument('--output_file')
args = parser.parse_args()


def random_subset_rows(input_file: str, num_of_samples: int,
                       random_seed: int, output_file: str) -> list[int]:
    """return subset indices

    Args:
        input_file (str): input file path
        num of samples (int): number of samples for selection
        random_seed (int): random seed used for selection
        output_file (str): output file of index list
    Returns:
        list[int]: index list of the selected samples.
    """
    with open(input_file, mode='r', encoding='utf-8') as f:
        numlines = len(f.readlines())
    indices = list(range(numlines))

    random.seed(random_seed)
    subset: list[int] = random.sample(indices, num_of_samples)

    with open(output_file, mode='w', encoding='utf-8') as f:
        for i in subset[:-1]:
            f.write(f'{i}\n')
        f.write(f'{subset[-1]}')

    return subset


if __name__ == '__main__':
    random_subset_rows(args.input_file, args.num_of_samples, args.random_seed, args.output_file)
