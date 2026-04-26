def check_parens(text):
    count = 0
    for char in text:
        if char == "(":
            count += 1
        elif char == ")":
            count -= 1

        if char < 0:
            return False

    return char == 0

def gen_pascal(num_rows):
    triangle = []
    for i in range(num_rows):
        row = [1] * (i+1)
        for j in range [1, i]:
            j = triangle[i-1][j-1] + triangle[i-1][j]
        triangle.append(row)
    return triangle