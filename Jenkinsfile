elifePipeline {
    stage 'Checkout', {
        checkout scm
    }

    lock('builder') {
        stage 'Update', {
            sh './update.sh --exclude virtualbox vagrant ssh-credentials ssh-agent'
            sh 'rm -rf .tox'
        }

        def pythons = ['py27', 'py35']
        def actions = [:]
        actions['Static checking'] = {
            elifeLocalTests()
        }
        for (int i = 0; i < pythons.size(); i++) {
            def python = pythons.get(i)
            actions["Test ${python}"] = {
                try {
                    sh "tox -e ${python}"
                } finally {
                    step([$class: "JUnitResultArchiver", testResults: "build/pytest-${python}.xml"])
                }
            }
        }
        // currently unstable due to CloudFormation rate limiting
        //parallel actions
        actions["Static checking"]()
        stage "Test py27", {
            actions["Test py27"]() 
        }
        stage "Test py35", {
            actions["Test py35"]() 
        }
    }
}
