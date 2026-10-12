"""Deterministic fictional names. No real identity dataset is used."""

GIVEN_NAMES = """Aisha Alex Amara Amir Anika Arjun Ben Cameron Carmen Chloe Daniel Dara David Elena Elias Emily Eva Farah Felix Gabriel Hana Hugo Imani Iris Isaac Isla Javier Jonah Jordan Julia Kai Leila Leo Lina Luca Maya Mateo Mei Miriam Nadia Naomi Nathan Nora Oliver Omar Priya Rafael Ravi Rina Robin Samira Sofia Theo Tom Vivian Yara Zara Zoya Zoe""".split()
FAMILY_NAMES = """Adler Ahmed Ali Alvarez Andersson Ashby Banerjee Bennett Brooks Campbell Castillo Chen Clarke Cohen Costa Davis Desai Dubois Edwards Ellis Fernandes Flores Foster Garcia Grant Green Gupta Haddad Hall Harris Hassan Hayes Huang Ibrahim Ito Ivanov Jackson Jensen Jones Khan Kim King Kowalski Kumar Laurent Lee Lewis Li Lopez Martin Meyer Mitchell Morgan Murphy Nguyen Novak Okafor Olsen Ortiz Park Patel Pereira Peters Price Rahman Reed Rivera Rossi Shah Silva Singh Smith Tan Taylor Thomas Tran Walker Wang Ward White Williams Wilson Wong Yilmaz Young Zhang""".split()


def fictional_name(index: int) -> str:
    # These combinations come only from fixed name lists. No identity, employer
    # directory or external dataset is consulted. Keep pay RNG calls unchanged.
    given = (index - 1) % len(GIVEN_NAMES)
    family = ((index - 1) // len(GIVEN_NAMES) + given * 13) % len(FAMILY_NAMES)
    return f"{GIVEN_NAMES[given]} {FAMILY_NAMES[family]}"
