import yara

yara_rules = b'''
'''


def yara_process_scan():
    rules = yara.compile(source=yara_rules)
    pass

def main():
    yara_process_scan()

if __name__ == "__main__":
    main()
