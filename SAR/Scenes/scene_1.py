import copy
from core import Controller
from Scenes.scene_initializer import BaseSceneInitializer
from misc import Arg

class SceneInitializer(BaseSceneInitializer):
    # 30 x 30, contains one of all w/ n_agents
    # one fire has 2 initially lighted locations, the other has 1 initially lighted location
    # names: ReservoirUtah, ReservoirYork, DepositFacility, CaldorFire, GreatFire, LostPersonTimmy, Alice, Bob, Charlie, David, Emma, Finn

    def __init__(self) -> None:
        self.name='area_900_2_fires_3_lights'
        self.params={
                'grid_size' : (30,30,1),

                'reservoirs' : [
                    Arg(tp='a',name='ReservoirUtah',position=(15,5)),
                    Arg(tp='b', name='ReservoirYork',position=(18,8))
                    ],
                'deposits' : [
                    Arg(name='DepositFacility', position=(23,12)),
                    ],
                'fires' : [
                    Arg(tp='a', amt_light=2, amt_regions=None, enclosing_grid=(6,6), name='CaldorFire', position=(2,2)),
                    Arg(tp='b', amt_light=1, amt_regions=None, enclosing_grid=(5,5), name='GreatFire', position=(20,16)) # v1 - has 1 light instead of two to prevent wild spread
                    ],
                'persons' : [
                    Arg(extra_load=0,find_probability=.3, name='LostPersonTimmy',position=(8,22)),
                    ],
                'agents' : [
                    Arg(position=(9,7)),
                    Arg(position=(17,14)),
                    Arg(position=(16,15)),
                    Arg(position=(24,3)),
                    Arg(position=(25,2)),
                    Arg(position=(25,25))
                    ]
                }
        self.task_timeout=1200
        super().__init__()
